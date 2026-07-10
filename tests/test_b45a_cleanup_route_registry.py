from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from claw_v2.bot import BotService
from claw_v2.dispatch import Route, RouteContext, dispatch_routes
from claw_v2.dispatch.matchers import CLEANUP_STATUS_MATCHER

# B4.5a — first route-registry migration (invariant
# b45a_cleanup_status_is_registry_invoked). cleanup_status is now invoked
# through registry Route data via a PER-SLOT bridge:
# dispatch_routes(self._cleanup_status_slot, route_ctx,
# on_decision=self._emit_route_decision) at the route's ORIGINAL order-locked
# slot (§5.1 row 7, between operational_status and the B4.5e owner bridge). It does
# NOT join _pre_brain_routes — that registry runs at the EARLY slot (rows
# 2-3) and moving cleanup there would reorder interception and telemetry
# rows. These tests lock: (1) the bridge's source position between its legacy
# neighbors; (2) no direct handler call remains in _handle_text_body; (3) the
# Route data (name from the declarative matcher); (4) the B4.4b live-smoke
# phrases behave identically through the registry path; (5) dispatch_decision
# kwargs stay byte-identical to the pre-registry inline emission. The B4.4b
# matcher corpus itself stays locked by
# tests/test_b44b_cleanup_matcher_pilot.py, unedited.


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
    "_cleanup_status_slot": "CLEANUP_BRIDGE",
    "_owner_delegation_slot": "OWNER_DELEGATION_BRIDGE",
}


def _ordered_markers() -> list[str]:
    """Source-order markers inside _handle_text_body, including the
    failure-summary, cleanup and owner per-slot registry bridges."""
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
    seen: list[str] = []
    for _, _, name in markers:
        if name not in seen:
            seen.append(name)
    return seen


class BridgeSlotPositionTests(unittest.TestCase):
    def test_bridge_sits_between_its_legacy_neighbors(self) -> None:
        order = _ordered_markers()
        self.assertIn("FAILURE_SUMMARY_BRIDGE", order, "B4.5d bridge disappeared")
        self.assertIn("CLEANUP_BRIDGE", order, "per-slot registry bridge not found")
        self.assertIn("OWNER_DELEGATION_BRIDGE", order, "B4.5e bridge disappeared")
        # B4.5d migrated failure_summary to its own per-slot bridge. The full
        # chain (FAILURE_SUMMARY_BRIDGE < OPERATIONAL_BRIDGE <
        # CLEANUP_BRIDGE < OWNER_DELEGATION_BRIDGE < imperative) is locked by
        # the B4.5d and B4.5e registry tests.
        failure_bridge = order.index("FAILURE_SUMMARY_BRIDGE")
        bridge = order.index("CLEANUP_BRIDGE")
        owner_bridge = order.index("OWNER_DELEGATION_BRIDGE")
        imperative = order.index("_maybe_handle_telegram_imperative_request")
        self.assertLess(failure_bridge, bridge, "cleanup must run after failure-summary")
        self.assertLess(bridge, owner_bridge, "cleanup must run before owner_delegation")
        self.assertLess(owner_bridge, imperative, "owner must run before imperative")

    def test_no_direct_handler_call_remains_in_handle_text_body(self) -> None:
        order = _ordered_markers()
        self.assertNotIn(
            "_maybe_handle_cleanup_status_query",
            order,
            "cleanup_status must be registry-invoked, not called directly",
        )

    def test_bridge_is_not_in_the_early_registry(self) -> None:
        # Joining _pre_brain_routes would move execution to the early slot
        # (rows 2-3) and reorder interception/telemetry — locked out here.
        src = inspect.getsource(BotService._build_pre_brain_routes)
        self.assertNotIn("cleanup", src)


class RouteDataTests(unittest.TestCase):
    def test_slot_is_a_single_route_named_from_the_matcher(self) -> None:
        stub = SimpleNamespace(_route_cleanup_status=lambda ctx: None)
        # Reproduce the slot construction contract from __init__ verbatim.
        slot = (Route(CLEANUP_STATUS_MATCHER.name, stub._route_cleanup_status),)
        init_src = inspect.getsource(BotService.__init__)
        self.assertIn("Route(CLEANUP_STATUS_MATCHER.name, self._route_cleanup_status)", init_src)
        self.assertIn("_cleanup_status_slot", init_src)
        self.assertEqual(len(slot), 1)
        self.assertEqual(slot[0].name, "cleanup_status")


def _make_stub() -> SimpleNamespace:
    """Minimal object carrying the real gate+renderer+adapter, with the
    response side's dependencies mocked (session state + approvals audit)."""
    stub = SimpleNamespace()
    stub.brain = SimpleNamespace(
        memory=SimpleNamespace(get_session_state=MagicMock(return_value={}))
    )
    stub.approvals = None
    stub._classify_pending_approvals = MagicMock(
        return_value={"still_needed": 0, "stale": 0, "duplicate": 0}
    )
    stub._maybe_handle_cleanup_status_query = BotService._maybe_handle_cleanup_status_query.__get__(
        stub
    )
    stub._route_cleanup_status = BotService._route_cleanup_status.__get__(stub)
    stub._emit_dispatch_decision = MagicMock()
    stub._emit_route_decision = BotService._emit_route_decision.__get__(stub)
    return stub


def _ctx(text: str) -> RouteContext:
    return RouteContext(user_id="u1", session_id="s1", text=text, stripped=text.strip())


class RegistryPathBehaviorTests(unittest.TestCase):
    # The exact positive/negative phrases live-smoked for B4.4b (PR #235).

    def test_positive_smoke_phrase_intercepts_through_registry(self) -> None:
        stub = _make_stub()
        outcome = dispatch_routes(
            (Route(CLEANUP_STATUS_MATCHER.name, stub._route_cleanup_status),),
            _ctx("¿ya limpiaste?"),
            on_decision=stub._emit_route_decision,
        )
        self.assertEqual(outcome.route, "intercepted")
        self.assertTrue(outcome.captured)
        self.assertIsInstance(outcome.response, str)
        self.assertEqual(outcome.reason, "cleanup_status_matched")
        kwargs = stub._emit_dispatch_decision.call_args.kwargs
        self.assertEqual(
            kwargs,
            {
                "handler": "cleanup_status",
                "route": "intercepted",
                "reason": "cleanup_status_matched",
                "session_id": "s1",
                "text": "¿ya limpiaste?",
                "captured": True,
            },
        )

    def test_negative_smoke_phrase_falls_through_registry(self) -> None:
        stub = _make_stub()
        outcome = dispatch_routes(
            (Route(CLEANUP_STATUS_MATCHER.name, stub._route_cleanup_status),),
            _ctx("ya limpiaste todo"),
            on_decision=stub._emit_route_decision,
        )
        self.assertEqual(outcome.captured, False)
        self.assertIsNone(outcome.response)
        kwargs = stub._emit_dispatch_decision.call_args.kwargs
        self.assertEqual(
            kwargs,
            {
                "handler": "cleanup_status",
                "route": "fall_through",
                "reason": "cleanup_status_no_match",
                "session_id": "s1",
                "text": "ya limpiaste todo",
                "captured": False,
            },
        )

    def test_adapter_reason_slugs_come_from_the_matcher(self) -> None:
        src = inspect.getsource(BotService._route_cleanup_status)
        self.assertIn("CLEANUP_STATUS_MATCHER.unmatched_reason", src)
        self.assertIn("CLEANUP_STATUS_MATCHER.matched_reason", src)
        self.assertNotIn('"cleanup_status_matched"', src)
        self.assertNotIn('"cleanup_status_no_match"', src)


if __name__ == "__main__":
    unittest.main()
