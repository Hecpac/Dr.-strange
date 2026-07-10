from __future__ import annotations

import ast
import inspect
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from claw_v2.bot import BotService
from claw_v2.dispatch import Route, RouteContext, dispatch_routes
from claw_v2.dispatch.matchers import (
    OPERATIONAL_FAILURE_SUMMARY_MATCHER,
    OPERATIONAL_STATUS_MATCHER,
)

# B4.5d — fourth route-registry migration (invariant
# b45d_failure_summary_is_registry_invoked). Only the _handle_text_body chain
# invocation moves to a one-Route per-slot bridge at its ORIGINAL §5.1 row 5
# slot. handle_multimodal stays on its legacy direct gate+telemetry block.
# This is the B4.5b guarded variant: dispatch first, quality guard at the call
# site, then _post_capture_intercepted. The adapter is pure.


def _handle_text_body_ast() -> ast.FunctionDef:
    source_file = inspect.getsourcefile(BotService)
    assert source_file is not None
    tree = ast.parse(Path(source_file).read_text(encoding="utf-8"))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_handle_text_body"
    )


def _walk_excluding_closures(node: ast.AST):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield child
        yield from _walk_excluding_closures(child)


_BRIDGE_MARKERS = {
    "_failure_summary_slot": "FAILURE_SUMMARY_BRIDGE",
    "_operational_status_slot": "OPERATIONAL_BRIDGE",
    "_cleanup_status_slot": "CLEANUP_BRIDGE",
}


def _marker_sequence() -> list[str]:
    """Source-order markers in _handle_text_body, without deduplication."""
    fn = _handle_text_body_ast()
    markers: list[tuple[int, int, str]] = []
    for stmt in fn.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in [stmt, *list(_walk_excluding_closures(stmt))]:
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                markers.append((node.lineno, node.col_offset, func.attr))
            elif isinstance(func, ast.Name):
                name = func.id
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
    def test_bridge_chain_sits_between_its_legacy_neighbors(self) -> None:
        order = _ordered_markers()
        self.assertIn("FAILURE_SUMMARY_BRIDGE", order, "failure-summary bridge not found")
        self.assertIn("OPERATIONAL_BRIDGE", order, "B4.5b bridge disappeared")
        self.assertIn("CLEANUP_BRIDGE", order, "B4.5a bridge disappeared")
        pending = order.index("_handle_pending_tasks_query")
        failure_bridge = order.index("FAILURE_SUMMARY_BRIDGE")
        op_bridge = order.index("OPERATIONAL_BRIDGE")
        cleanup_bridge = order.index("CLEANUP_BRIDGE")
        owner = order.index("_maybe_handle_owner_delegation_request")
        self.assertLess(pending, failure_bridge, "failure bridge must run after pending_tasks")
        self.assertLess(failure_bridge, op_bridge, "failure bridge must run before operational")
        self.assertLess(op_bridge, cleanup_bridge, "operational must run before cleanup")
        self.assertLess(cleanup_bridge, owner, "cleanup must run before owner_delegation")

    def test_bridge_dispatches_exactly_once(self) -> None:
        self.assertEqual(
            _marker_sequence().count("FAILURE_SUMMARY_BRIDGE"),
            1,
            "failure-summary bridge must dispatch exactly once per text turn",
        )

    def test_no_direct_handler_call_remains_in_handle_text_body(self) -> None:
        self.assertNotIn(
            "_maybe_handle_operational_failure_summary",
            _ordered_markers(),
            "failure summary must be registry-invoked in _handle_text_body",
        )

    def test_bridge_is_not_in_the_early_registry(self) -> None:
        src = inspect.getsource(BotService._build_pre_brain_routes)
        self.assertNotIn("failure_summary", src)


class RouteDataTests(unittest.TestCase):
    def test_slot_is_a_single_route_named_from_the_matcher(self) -> None:
        stub = SimpleNamespace(_route_operational_failure_summary=lambda ctx: None)
        slot = (
            Route(
                OPERATIONAL_FAILURE_SUMMARY_MATCHER.name,
                stub._route_operational_failure_summary,
            ),
        )
        init_src = inspect.getsource(BotService.__init__)
        self.assertIn("_failure_summary_slot", init_src)
        self.assertEqual(len(slot), 1)
        self.assertEqual(slot[0].name, "operational_failure_summary")

    def test_real_slot_assignment_is_a_one_route_tuple(self) -> None:
        tree = ast.parse(textwrap.dedent(inspect.getsource(BotService.__init__)))
        assignments = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Attribute) and target.attr == "_failure_summary_slot"
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
        ]
        self.assertEqual(len(assignments), 1)
        value = assignments[0].value
        self.assertIsInstance(value, ast.Tuple)
        self.assertEqual(len(value.elts), 1)
        route_call = value.elts[0]
        self.assertIsInstance(route_call, ast.Call)
        self.assertIsInstance(route_call.func, ast.Name)
        self.assertEqual(route_call.func.id, "Route")
        self.assertEqual(len(route_call.args), 2)
        matcher_name, adapter = route_call.args
        self.assertIsInstance(matcher_name, ast.Attribute)
        self.assertEqual(matcher_name.attr, "name")
        self.assertIsInstance(matcher_name.value, ast.Name)
        self.assertEqual(matcher_name.value.id, "OPERATIONAL_FAILURE_SUMMARY_MATCHER")
        self.assertIsInstance(adapter, ast.Attribute)
        self.assertEqual(adapter.attr, "_route_operational_failure_summary")
        self.assertIsInstance(adapter.value, ast.Name)
        self.assertEqual(adapter.value.id, "self")


def _make_stub() -> SimpleNamespace:
    stub = SimpleNamespace()
    stub._format_operational_failure_summary = MagicMock(
        return_value="Resumen operativo de fallos de hoy"
    )
    stub._maybe_handle_operational_failure_summary = (
        BotService._maybe_handle_operational_failure_summary.__get__(stub)
    )
    stub._route_operational_failure_summary = BotService._route_operational_failure_summary.__get__(
        stub
    )
    stub._emit_dispatch_decision = MagicMock()
    stub._emit_route_decision = BotService._emit_route_decision.__get__(stub)
    return stub


def _ctx(text: str) -> RouteContext:
    return RouteContext(user_id="u1", session_id="s1", text=text, stripped=text.strip())


def _slot(stub: SimpleNamespace) -> tuple[Route, ...]:
    return (
        Route(
            OPERATIONAL_FAILURE_SUMMARY_MATCHER.name,
            stub._route_operational_failure_summary,
        ),
    )


class RegistryPathBehaviorTests(unittest.TestCase):
    def test_positive_smoke_phrases_intercept_through_registry(self) -> None:
        for phrase in ("hoy hubo errores", "porque fallaste"):
            with self.subTest(phrase=phrase):
                stub = _make_stub()
                outcome = dispatch_routes(
                    _slot(stub), _ctx(phrase), on_decision=stub._emit_route_decision
                )
                self.assertEqual(outcome.route, "intercepted")
                self.assertTrue(outcome.captured)
                self.assertEqual(outcome.response, "Resumen operativo de fallos de hoy")
                self.assertEqual(outcome.reason, "operational_failure_summary_matched")
                self.assertEqual(outcome.store_memory_limit, 3000)
                stub._format_operational_failure_summary.assert_called_once_with("s1")
                self.assertEqual(
                    stub._emit_dispatch_decision.call_args.kwargs,
                    {
                        "handler": "operational_failure_summary",
                        "route": "intercepted",
                        "reason": "operational_failure_summary_matched",
                        "session_id": "s1",
                        "text": phrase,
                        "captured": True,
                    },
                )

    def test_negative_smoke_phrase_falls_through_registry(self) -> None:
        stub = _make_stub()
        phrase = "porque fallaste la tarea"
        outcome = dispatch_routes(_slot(stub), _ctx(phrase), on_decision=stub._emit_route_decision)
        self.assertEqual(outcome.route, "fall_through")
        self.assertFalse(outcome.captured)
        self.assertIsNone(outcome.response)
        stub._format_operational_failure_summary.assert_not_called()
        self.assertEqual(
            stub._emit_dispatch_decision.call_args.kwargs,
            {
                "handler": "operational_failure_summary",
                "route": "fall_through",
                "reason": "operational_failure_summary_no_match",
                "session_id": "s1",
                "text": phrase,
                "captured": False,
            },
        )

    def test_a1_collision_variant_falls_through_to_operational_status(self) -> None:
        phrase = "hola status hoy errores; no continuemos"
        self.assertFalse(OPERATIONAL_FAILURE_SUMMARY_MATCHER.match(phrase))
        self.assertTrue(OPERATIONAL_STATUS_MATCHER.match(phrase))
        stub = _make_stub()
        outcome = dispatch_routes(_slot(stub), _ctx(phrase), on_decision=stub._emit_route_decision)
        self.assertEqual(outcome.route, "fall_through")
        self.assertEqual(
            stub._emit_dispatch_decision.call_args.kwargs,
            {
                "handler": "operational_failure_summary",
                "route": "fall_through",
                "reason": "operational_failure_summary_no_match",
                "session_id": "s1",
                "text": phrase,
                "captured": False,
            },
        )

    def test_adapter_passes_only_stripped_text_and_session_to_legacy_gate(self) -> None:
        stub = SimpleNamespace(
            _maybe_handle_operational_failure_summary=MagicMock(return_value=None)
        )
        stub._route_operational_failure_summary = (
            BotService._route_operational_failure_summary.__get__(stub)
        )
        outcome = stub._route_operational_failure_summary(_ctx("  no coincide  "))
        stub._maybe_handle_operational_failure_summary.assert_called_once_with(
            "no coincide", session_id="s1"
        )
        self.assertEqual(outcome.route, "fall_through")

    def test_adapter_reason_slugs_come_from_the_matcher(self) -> None:
        src = inspect.getsource(BotService._route_operational_failure_summary)
        self.assertIn("OPERATIONAL_FAILURE_SUMMARY_MATCHER.unmatched_reason", src)
        self.assertIn("OPERATIONAL_FAILURE_SUMMARY_MATCHER.matched_reason", src)
        self.assertNotIn('"operational_failure_summary_matched"', src)
        self.assertNotIn('"operational_failure_summary_no_match"', src)


class QualityGuardPreservationTests(unittest.TestCase):
    def test_call_site_keeps_guard_outside_the_adapter(self) -> None:
        body_src = inspect.getsource(BotService._handle_text_body)
        self.assertIn('source="operational_failure_summary"', body_src)
        self.assertIn("failure_summary_outcome.response", body_src)
        self.assertIn(
            "assistant_limit=failure_summary_outcome.store_memory_limit",
            body_src,
        )
        adapter_src = inspect.getsource(BotService._route_operational_failure_summary)
        self.assertNotIn("_quality_guard_response", adapter_src)

    def test_guard_runs_after_dispatch_and_before_post_capture(self) -> None:
        body_src = inspect.getsource(BotService._handle_text_body)
        bridge_at = body_src.index("self._failure_summary_slot")
        guard_at = body_src.index('source="operational_failure_summary"')
        post_capture_at = body_src.index("self._post_capture_intercepted", guard_at)
        self.assertLess(bridge_at, guard_at)
        self.assertLess(guard_at, post_capture_at)


if __name__ == "__main__":
    unittest.main()
