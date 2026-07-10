from __future__ import annotations

import ast
import inspect
import textwrap
import unittest
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from claw_v2.bot import BotService
from claw_v2.bot_helpers import detect_owner_delegation
from claw_v2.dispatch import Route, RouteContext, RouteOutcome, dispatch_routes
from claw_v2.dispatch.matchers import OWNER_DELEGATION_MATCHER
from claw_v2.state_handler import DelegatedObjectiveResolution

# B4.5e — owner_delegation moves into a one-Route per-slot registry bridge at
# its ORIGINAL text-chain position: after the cleanup bridge and before the
# telegram imperative. The legacy handler remains the rich-intent owner and
# keeps its accepted matcher+detector double call and dynamic inner decision.
# The registry callback adds the static outer decision exactly as the removed
# inline block did. Match: dynamic then static (two decisions). No match: only
# the static fall-through decision. This is the B4.5a no-quality-guard shape.


def _handle_text_body_ast() -> ast.FunctionDef:
    source_file = inspect.getsourcefile(BotService)
    assert source_file is not None
    tree = ast.parse(Path(source_file).read_text(encoding="utf-8"))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_handle_text_body"
    )


def _function_ast(owner: object) -> ast.FunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(owner)))
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef))


def _walk_excluding_closures(node: ast.AST):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield child
        yield from _walk_excluding_closures(child)


def _call_path(call_node: ast.Call) -> str | None:
    parts: list[str] = []
    node: ast.expr = call_node.func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _expression_dump(source: str) -> str:
    return ast.dump(ast.parse(source, mode="eval").body, include_attributes=False)


_BRIDGE_MARKERS = {
    "_failure_summary_slot": "FAILURE_SUMMARY_BRIDGE",
    "_operational_status_slot": "OPERATIONAL_BRIDGE",
    "_cleanup_status_slot": "CLEANUP_BRIDGE",
    "_owner_delegation_slot": "OWNER_DELEGATION_BRIDGE",
}


def _marker_sequence() -> list[str]:
    markers: list[tuple[int, int, str]] = []
    for stmt in _handle_text_body_ast().body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in [stmt, *list(_walk_excluding_closures(stmt))]:
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                markers.append((node.lineno, node.col_offset, node.func.attr))
            elif isinstance(node.func, ast.Name):
                name = node.func.id
                if name == "dispatch_routes" and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Attribute) and first.attr in _BRIDGE_MARKERS:
                        markers.append((node.lineno, node.col_offset, _BRIDGE_MARKERS[first.attr]))
                    else:
                        markers.append((node.lineno, node.col_offset, "dispatch_routes"))
    markers.sort()
    return [name for _, _, name in markers]


def _ordered_markers() -> list[str]:
    seen: list[str] = []
    for name in _marker_sequence():
        if name not in seen:
            seen.append(name)
    return seen


class BridgeSlotPositionTests(unittest.TestCase):
    def test_bridge_sits_between_cleanup_and_telegram_imperative(self) -> None:
        order = _ordered_markers()
        self.assertIn("CLEANUP_BRIDGE", order, "B4.5a bridge disappeared")
        self.assertIn("OWNER_DELEGATION_BRIDGE", order, "owner bridge not found")
        cleanup = order.index("CLEANUP_BRIDGE")
        owner = order.index("OWNER_DELEGATION_BRIDGE")
        imperative = order.index("_maybe_handle_telegram_imperative_request")
        self.assertLess(cleanup, owner, "owner bridge must run after cleanup")
        self.assertLess(owner, imperative, "owner bridge must run before imperative")

    def test_owner_bridge_dispatches_exactly_once(self) -> None:
        self.assertEqual(
            _marker_sequence().count("OWNER_DELEGATION_BRIDGE"),
            1,
            "owner bridge must dispatch exactly once per text turn",
        )

    def test_no_direct_owner_handler_call_remains_in_handle_text_body(self) -> None:
        self.assertNotIn(
            "_maybe_handle_owner_delegation_request",
            _ordered_markers(),
            "owner delegation must be registry-invoked in _handle_text_body",
        )

    def test_bridge_call_has_exact_registry_callback_shape(self) -> None:
        candidates = [
            node
            for node in ast.walk(_handle_text_body_ast())
            if isinstance(node, ast.Call)
            and _call_path(node) == "dispatch_routes"
            and node.args
            and ast.dump(node.args[0], include_attributes=False)
            == _expression_dump("self._owner_delegation_slot")
        ]
        self.assertEqual(len(candidates), 1)
        call_node = candidates[0]
        self.assertEqual(
            [ast.dump(arg, include_attributes=False) for arg in call_node.args],
            [
                _expression_dump("self._owner_delegation_slot"),
                _expression_dump("route_ctx"),
            ],
        )
        self.assertEqual(
            {
                keyword.arg: ast.dump(keyword.value, include_attributes=False)
                for keyword in call_node.keywords
            },
            {"on_decision": _expression_dump("self._emit_route_decision")},
        )

    def test_bridge_is_not_in_the_early_registry(self) -> None:
        self.assertNotIn(
            "owner_delegation",
            inspect.getsource(BotService._build_pre_brain_routes),
        )


class RouteDataTests(unittest.TestCase):
    def test_real_slot_assignment_is_a_one_route_tuple_named_from_matcher(self) -> None:
        tree = ast.parse(textwrap.dedent(inspect.getsource(BotService.__init__)))
        assignments = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Attribute) and target.attr == "_owner_delegation_slot"
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
        ]
        self.assertEqual(len(assignments), 1)
        value = assignments[0].value
        self.assertIsInstance(value, ast.Tuple)
        self.assertEqual(len(value.elts), 1)
        route_call = value.elts[0]
        self.assertIsInstance(route_call, ast.Call)
        self.assertEqual(_call_path(route_call), "Route")
        self.assertEqual(len(route_call.args), 2)
        self.assertEqual(
            ast.dump(route_call.args[0], include_attributes=False),
            _expression_dump("OWNER_DELEGATION_MATCHER.name"),
        )
        self.assertEqual(
            ast.dump(route_call.args[1], include_attributes=False),
            _expression_dump("self._route_owner_delegation"),
        )


def _resolution() -> DelegatedObjectiveResolution:
    return DelegatedObjectiveResolution(
        objective=None,
        resolution_source=None,
        mode="chat",
        is_risky=False,
        clarifying_question="Necesito el objetivo concreto.",
    )


def _make_stub() -> SimpleNamespace:
    stub = SimpleNamespace()
    stub.observe = None
    stub._state_handler = SimpleNamespace(
        resolve_delegated_objective=MagicMock(return_value=_resolution())
    )
    stub._maybe_handle_owner_delegation_request = (
        BotService._maybe_handle_owner_delegation_request.__get__(stub)
    )
    stub._route_owner_delegation = BotService._route_owner_delegation.__get__(stub)
    stub._emit_dispatch_decision = MagicMock()
    stub._emit_route_decision = BotService._emit_route_decision.__get__(stub)
    return stub


def _ctx(text: str, *, runtime_channel: str | None = "telegram") -> RouteContext:
    return RouteContext(
        user_id="u1",
        session_id="s1",
        text=text,
        stripped=text.strip(),
        runtime_channel=runtime_channel,
    )


def _slot(stub: SimpleNamespace) -> tuple[Route, ...]:
    return (Route(OWNER_DELEGATION_MATCHER.name, stub._route_owner_delegation),)


class RegistryPathTelemetryTests(unittest.TestCase):
    def test_match_corpus_emits_dynamic_then_static_exactly(self) -> None:
        cases = (
            ("córrelo tú mismo", "execution"),
            ("decide tú", "decision"),
            ("te toca a ti", "no_manual_work"),
        )
        for phrase, kind in cases:
            with self.subTest(phrase=phrase, kind=kind):
                stub = _make_stub()
                outcome = dispatch_routes(
                    _slot(stub),
                    _ctx(phrase),
                    on_decision=stub._emit_route_decision,
                )
                self.assertEqual(outcome.route, "intercepted")
                self.assertTrue(outcome.captured)
                self.assertEqual(outcome.response, "Necesito el objetivo concreto.")
                self.assertEqual(outcome.reason, OWNER_DELEGATION_MATCHER.matched_reason)
                self.assertEqual(outcome.store_memory_limit, 4000)
                dynamic_reason = f"owner_delegation:{kind}"
                self.assertEqual(
                    stub._emit_dispatch_decision.call_args_list,
                    [
                        call(
                            handler="owner_delegation",
                            route="intercepted",
                            reason=dynamic_reason,
                            session_id="s1",
                            text=phrase,
                            captured=True,
                            matched_pattern=dynamic_reason,
                        ),
                        call(
                            handler="owner_delegation",
                            route="intercepted",
                            reason="owner_delegation_matched",
                            session_id="s1",
                            text=phrase,
                            captured=True,
                        ),
                    ],
                )

    def test_no_match_corpus_emits_one_static_fallthrough_decision(self) -> None:
        for phrase in ("hola", "gracias", "abre Linear"):
            with self.subTest(phrase=phrase):
                stub = _make_stub()
                outcome = dispatch_routes(
                    _slot(stub),
                    _ctx(phrase),
                    on_decision=stub._emit_route_decision,
                )
                self.assertEqual(outcome.route, "fall_through")
                self.assertFalse(outcome.captured)
                self.assertIsNone(outcome.response)
                self.assertEqual(
                    stub._emit_dispatch_decision.call_args_list,
                    [
                        call(
                            handler="owner_delegation",
                            route="fall_through",
                            reason="owner_delegation_no_match",
                            session_id="s1",
                            text=phrase,
                            captured=False,
                        )
                    ],
                )

    def test_registry_match_keeps_exactly_two_detector_calls_not_three(self) -> None:
        stub = _make_stub()
        with (
            patch(
                "claw_v2.dispatch.matchers.detect_owner_delegation",
                wraps=detect_owner_delegation,
            ) as matcher_detector,
            patch(
                "claw_v2.bot.detect_owner_delegation",
                wraps=detect_owner_delegation,
            ) as rich_intent_detector,
        ):
            dispatch_routes(
                _slot(stub),
                _ctx("decide tú"),
                on_decision=stub._emit_route_decision,
            )
        self.assertEqual(matcher_detector.call_count, 1)
        self.assertEqual(rich_intent_detector.call_count, 1)

    def test_registry_no_match_runs_only_the_matcher_detector(self) -> None:
        stub = _make_stub()
        with (
            patch(
                "claw_v2.dispatch.matchers.detect_owner_delegation",
                wraps=detect_owner_delegation,
            ) as matcher_detector,
            patch(
                "claw_v2.bot.detect_owner_delegation",
                wraps=detect_owner_delegation,
            ) as rich_intent_detector,
        ):
            dispatch_routes(
                _slot(stub),
                _ctx("hola"),
                on_decision=stub._emit_route_decision,
            )
        self.assertEqual(matcher_detector.call_count, 1)
        self.assertEqual(rich_intent_detector.call_count, 0)


class AdapterAndCallSiteTests(unittest.TestCase):
    def test_adapter_passes_exact_legacy_arguments(self) -> None:
        legacy = MagicMock(return_value=None)
        stub = SimpleNamespace(_maybe_handle_owner_delegation_request=legacy)
        stub._route_owner_delegation = BotService._route_owner_delegation.__get__(stub)
        outcome = stub._route_owner_delegation(_ctx("  hola  ", runtime_channel="telegram"))
        legacy.assert_called_once_with(
            "hola",
            session_id="s1",
            runtime_channel="telegram",
        )
        self.assertEqual(outcome.route, "fall_through")
        self.assertEqual(outcome.reason, OWNER_DELEGATION_MATCHER.unmatched_reason)

    def test_adapter_intercept_preserves_matcher_slug_and_memory_limit(self) -> None:
        legacy = MagicMock(return_value="captured")
        stub = SimpleNamespace(_maybe_handle_owner_delegation_request=legacy)
        stub._route_owner_delegation = BotService._route_owner_delegation.__get__(stub)
        outcome = stub._route_owner_delegation(_ctx("decide tú"))
        self.assertEqual(outcome.route, "intercepted")
        self.assertEqual(outcome.response, "captured")
        self.assertEqual(outcome.reason, OWNER_DELEGATION_MATCHER.matched_reason)
        self.assertEqual(outcome.store_memory_limit, 4000)

    def test_adapter_is_pure_and_reason_slugs_come_only_from_matcher(self) -> None:
        src = inspect.getsource(BotService._route_owner_delegation)
        self.assertIn("OWNER_DELEGATION_MATCHER.unmatched_reason", src)
        self.assertIn("OWNER_DELEGATION_MATCHER.matched_reason", src)
        self.assertNotIn('"owner_delegation_matched"', src)
        self.assertNotIn('"owner_delegation_no_match"', src)
        self.assertNotIn("detect_owner_delegation", src)
        self.assertNotIn("_emit_dispatch_decision", src)
        self.assertNotIn("_quality_guard_response", src)
        self.assertNotIn("_post_capture_intercepted", src)
        call_paths = {
            _call_path(node)
            for node in ast.walk(_function_ast(BotService._route_owner_delegation))
            if isinstance(node, ast.Call)
        }
        self.assertNotIn("OWNER_DELEGATION_MATCHER.match", call_paths)

    def test_call_site_uses_outcome_limit_without_quality_guard(self) -> None:
        body_src = inspect.getsource(BotService._handle_text_body)
        start = body_src.index("owner_delegation_outcome = dispatch_routes")
        end = body_src.index("telegram_imperative_response", start)
        owner_block = body_src[start:end]
        self.assertIn("self._owner_delegation_slot", owner_block)
        self.assertIn("self._post_capture_intercepted", owner_block)
        self.assertIn(
            "assistant_limit=owner_delegation_outcome.store_memory_limit",
            owner_block,
        )
        self.assertNotIn("_quality_guard_response", owner_block)
        self.assertNotIn("_maybe_handle_owner_delegation_request", owner_block)


class FrozenRouteContractTests(unittest.TestCase):
    def test_route_context_and_outcome_fields_are_unchanged(self) -> None:
        self.assertEqual(
            [field.name for field in fields(RouteContext)],
            ["user_id", "session_id", "text", "stripped", "runtime_channel", "metadata"],
        )
        self.assertEqual(
            [field.name for field in fields(RouteOutcome)],
            ["route", "response", "reason", "captured", "extra", "store_memory_limit"],
        )


if __name__ == "__main__":
    unittest.main()
