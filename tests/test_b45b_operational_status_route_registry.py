from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from claw_v2.bot import BotService
from claw_v2.dispatch import Route, RouteContext, dispatch_routes
from claw_v2.dispatch.matchers import OPERATIONAL_STATUS_MATCHER

# B4.5b — second route-registry migration (invariant
# b45b_operational_status_is_registry_invoked). operational_status is now
# invoked through registry Route data via a PER-SLOT bridge:
# dispatch_routes(self._operational_status_slot, route_ctx,
# on_decision=self._emit_route_decision) at the route's ORIGINAL order-locked
# slot (§5.1 row 6, between the B4.5d failure-summary bridge and B4.5a cleanup
# bridge). It does NOT join _pre_brain_routes (the early slot, rows 2-3).
# Delta vs B4.5a: this route has a quality guard. The guard stays AT THE CALL
# SITE (after dispatch_routes, before _post_capture_intercepted) so the legacy
# event order — dispatch_decision first, then a possible
# quality_guard_triggered — is preserved exactly; the adapter itself is pure.
# These tests lock: (1) the full bridge chain position
# (FAILURE_SUMMARY_BRIDGE < OPERATIONAL_BRIDGE < CLEANUP_BRIDGE < owner);
# (2) no direct handler call remains in _handle_text_body; (3) the Route data
# (name from the declarative matcher); (4) the B4.4c live-smoke phrases behave
# identically through the registry path; (5) dispatch_decision kwargs stay
# byte-identical to the pre-registry inline emission; (6) the call-site
# quality guard survives with its byte-locked source slug. The B4.4c matcher
# corpus itself stays locked by
# tests/test_b44c_operational_status_matcher_pilot.py, unedited.


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


def _ordered_markers() -> list[str]:
    """Source-order markers inside _handle_text_body: legacy dispatch-call
    names plus a synthetic marker per per-slot registry bridge (the
    dispatch_routes calls whose routes argument is a *_slot attribute)."""
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
    def test_bridge_chain_sits_between_its_legacy_neighbors(self) -> None:
        order = _ordered_markers()
        self.assertIn("FAILURE_SUMMARY_BRIDGE", order, "B4.5d bridge disappeared")
        self.assertIn("OPERATIONAL_BRIDGE", order, "per-slot registry bridge not found")
        self.assertIn("CLEANUP_BRIDGE", order, "B4.5a bridge disappeared")
        failure_bridge = order.index("FAILURE_SUMMARY_BRIDGE")
        op_bridge = order.index("OPERATIONAL_BRIDGE")
        cleanup_bridge = order.index("CLEANUP_BRIDGE")
        owner = order.index("_maybe_handle_owner_delegation_request")
        self.assertLess(failure_bridge, op_bridge, "operational must run after failure-summary")
        self.assertLess(op_bridge, cleanup_bridge, "bridge must run before the cleanup bridge")
        self.assertLess(cleanup_bridge, owner, "cleanup bridge must run before owner_delegation")

    def test_no_direct_handler_call_remains_in_handle_text_body(self) -> None:
        order = _ordered_markers()
        self.assertNotIn(
            "_maybe_handle_operational_status",
            order,
            "operational_status must be registry-invoked, not called directly",
        )

    def test_bridge_is_not_in_the_early_registry(self) -> None:
        # Joining _pre_brain_routes would move execution to the early slot
        # (rows 2-3) and reorder interception/telemetry — locked out here.
        # (operational_alert legitimately lives there; it is a different name.)
        src = inspect.getsource(BotService._build_pre_brain_routes)
        self.assertNotIn("operational_status", src)


class RouteDataTests(unittest.TestCase):
    def test_slot_is_a_single_route_named_from_the_matcher(self) -> None:
        stub = SimpleNamespace(_route_operational_status=lambda ctx: None)
        # Reproduce the slot construction contract from __init__ verbatim.
        slot = (Route(OPERATIONAL_STATUS_MATCHER.name, stub._route_operational_status),)
        init_src = inspect.getsource(BotService.__init__)
        self.assertIn(
            "Route(OPERATIONAL_STATUS_MATCHER.name, self._route_operational_status)", init_src
        )
        self.assertIn("_operational_status_slot", init_src)
        self.assertEqual(len(slot), 1)
        self.assertEqual(slot[0].name, "operational_status")


def _make_stub() -> SimpleNamespace:
    """Minimal object carrying the real gate+renderer+adapter, with the
    response side's dependencies stubbed (ledger, config, runtime probe,
    approvals)."""
    stub = SimpleNamespace()
    stub.task_ledger = None
    stub.config = None
    stub.approvals = None
    stub._runtime_alive = MagicMock(return_value=False)
    stub._maybe_handle_operational_status = BotService._maybe_handle_operational_status.__get__(
        stub
    )
    stub._route_operational_status = BotService._route_operational_status.__get__(stub)
    stub._emit_dispatch_decision = MagicMock()
    stub._emit_route_decision = BotService._emit_route_decision.__get__(stub)
    return stub


def _ctx(text: str) -> RouteContext:
    return RouteContext(user_id="u1", session_id="s1", text=text, stripped=text.strip())


def _slot(stub: SimpleNamespace) -> tuple[Route, ...]:
    return (Route(OPERATIONAL_STATUS_MATCHER.name, stub._route_operational_status),)


class RegistryPathBehaviorTests(unittest.TestCase):
    # The exact positive/negative/overlap phrases live-smoked for B4.4c
    # (PR #236).

    def test_positive_smoke_phrases_intercept_through_registry(self) -> None:
        for phrase in ("status", "¿estás vivo?"):
            with self.subTest(phrase=phrase):
                stub = _make_stub()
                outcome = dispatch_routes(
                    _slot(stub), _ctx(phrase), on_decision=stub._emit_route_decision
                )
                self.assertEqual(outcome.route, "intercepted")
                self.assertTrue(outcome.captured)
                self.assertIsInstance(outcome.response, str)
                self.assertIn("Estoy vivo.", outcome.response)
                self.assertEqual(outcome.reason, "operational_status_matched")
                # Locks legacy assistant_limit=2000 parity: the adapter must
                # not override the RouteOutcome default (minimax P3-1).
                self.assertEqual(outcome.store_memory_limit, 2000)
                kwargs = stub._emit_dispatch_decision.call_args.kwargs
                self.assertEqual(
                    kwargs,
                    {
                        "handler": "operational_status",
                        "route": "intercepted",
                        "reason": "operational_status_matched",
                        "session_id": "s1",
                        "text": phrase,
                        "captured": True,
                    },
                )

    def test_negative_smoke_phrase_falls_through_registry(self) -> None:
        stub = _make_stub()
        outcome = dispatch_routes(
            _slot(stub), _ctx("dame el status del deploy"), on_decision=stub._emit_route_decision
        )
        self.assertEqual(outcome.captured, False)
        self.assertIsNone(outcome.response)
        kwargs = stub._emit_dispatch_decision.call_args.kwargs
        self.assertEqual(
            kwargs,
            {
                "handler": "operational_status",
                "route": "fall_through",
                "reason": "operational_status_no_match",
                "session_id": "s1",
                "text": "dame el status del deploy",
                "captured": False,
            },
        )

    def test_overlap_phrase_still_falls_through_to_change_status(self) -> None:
        # PR #236 overlap probe: "estado de los cambios" is NOT operational —
        # it must keep falling through so change_status (a later slot)
        # intercepts, exactly as in the legacy chain.
        stub = _make_stub()
        outcome = dispatch_routes(
            _slot(stub), _ctx("estado de los cambios"), on_decision=stub._emit_route_decision
        )
        self.assertEqual(outcome.route, "fall_through")
        self.assertFalse(outcome.captured)
        # The per-route reason travels on the decision callback (the returned
        # aggregate outcome says "no_route_matched" when nothing intercepts).
        kwargs = stub._emit_dispatch_decision.call_args.kwargs
        self.assertEqual(kwargs["reason"], "operational_status_no_match")
        self.assertEqual(kwargs["route"], "fall_through")

    def test_adapter_reason_slugs_come_from_the_matcher(self) -> None:
        src = inspect.getsource(BotService._route_operational_status)
        self.assertIn("OPERATIONAL_STATUS_MATCHER.unmatched_reason", src)
        self.assertIn("OPERATIONAL_STATUS_MATCHER.matched_reason", src)
        self.assertNotIn('"operational_status_matched"', src)
        self.assertNotIn('"operational_status_no_match"', src)


class QualityGuardPreservationTests(unittest.TestCase):
    def test_call_site_still_quality_guards_the_intercepted_response(self) -> None:
        # The guard must stay AT THE CALL SITE (not inside the adapter): the
        # legacy chain emits dispatch_decision first and only then may emit
        # quality_guard_triggered. Moving the guard into the adapter would
        # swap that event order whenever the guard fires.
        body_src = inspect.getsource(BotService._handle_text_body)
        self.assertIn('source="operational_status"', body_src)
        self.assertIn("operational_status_outcome.response", body_src)
        adapter_src = inspect.getsource(BotService._route_operational_status)
        self.assertNotIn("_quality_guard_response", adapter_src)

    def test_guard_runs_after_the_bridge_dispatch(self) -> None:
        # Locks the event ORDER, not just presence (minimax P3-5): the guard
        # call must sit AFTER the dispatch_routes bridge in source, so
        # dispatch_decision is always emitted before a possible
        # quality_guard_triggered — the legacy order.
        body_src = inspect.getsource(BotService._handle_text_body)
        bridge_at = body_src.index("self._operational_status_slot")
        guard_at = body_src.index('source="operational_status"')
        self.assertLess(
            bridge_at,
            guard_at,
            "quality guard must run after the registry dispatch, never before",
        )


if __name__ == "__main__":
    unittest.main()
