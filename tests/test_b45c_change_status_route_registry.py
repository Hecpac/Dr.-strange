from __future__ import annotations

import ast
import inspect
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

from claw_v2.bot import BotService
from claw_v2.dispatch import Route, RouteContext, dispatch_routes
from claw_v2.dispatch.matchers import CHANGE_STATUS_MATCHER

# B4.5c — third route-registry migration (invariant
# b45c_change_status_is_registry_invoked). change_status is now invoked
# through registry Route data via a PER-SLOT bridge:
# dispatch_routes(self._change_status_slot, route_ctx,
# on_decision=self._emit_route_decision) at the route's ORIGINAL order-locked
# slot (§5.1 row 10, between task_intent and the meta-introspection guard /
# capability_route). It does NOT join _pre_brain_routes — that registry runs
# at the EARLY slot (rows 2-3) and moving change_status there would reorder
# interception and telemetry rows. Same no-guard variant as B4.5a: the legacy
# call site never quality-guarded this response, so the bridge call site goes
# straight to _post_capture_intercepted. These tests lock: (1) the bridge's
# source position between its legacy neighbors; (2) no direct handler call
# remains in _handle_text_body; (3) the Route data (name from the declarative
# matcher); (4) the B4.4a corpus phrases (including the PR #246 estados-plural
# widening) behave identically through the registry path; (5)
# dispatch_decision kwargs stay byte-identical to the pre-registry inline
# emission; (6) the overlap phrase owned by operational_status (an EARLIER
# slot) keeps falling through this bridge. The B4.4a matcher corpus itself
# stays locked by tests/test_b44a_declarative_matcher_pilot.py, unedited.


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


def _marker_sequence() -> list[str]:
    """Source-order markers inside _handle_text_body, NOT deduplicated:
    legacy dispatch-call names, a synthetic marker for the change_status
    registry bridge (the dispatch_routes call whose routes argument is
    self._change_status_slot), and a META_GUARD marker for the
    detect_meta_introspection_request call — the bridge must stay strictly
    before that guard's early return (§5.1 rows 10 vs 11).
    """
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
                    if isinstance(first, ast.Attribute) and first.attr == "_change_status_slot":
                        markers.append((node.lineno, node.col_offset, "CHANGE_STATUS_BRIDGE"))
                    else:
                        markers.append((node.lineno, node.col_offset, "dispatch_routes"))
                elif name == "detect_meta_introspection_request":
                    markers.append((node.lineno, node.col_offset, "META_GUARD"))
    markers.sort()
    return [name for _, _, name in markers]


def _ordered_markers() -> list[str]:
    """First-occurrence order of _marker_sequence()."""
    seen: list[str] = []
    for name in _marker_sequence():
        if name not in seen:
            seen.append(name)
    return seen


class BridgeSlotPositionTests(unittest.TestCase):
    def test_bridge_sits_between_its_legacy_neighbors(self) -> None:
        order = _ordered_markers()
        self.assertIn("CHANGE_STATUS_BRIDGE", order, "per-slot registry bridge not found")
        # §5.1 row 10: change_status runs after task_intent (row 9) and
        # before the meta-introspection guard + capability_route (row 11).
        # The guard has its own early return to the brain — a bridge moved
        # past it would silently lose interception for guard-matched turns,
        # so the guard is locked as an explicit neighbor, not just implied.
        task_intent = order.index("_maybe_handle_task_intent")
        bridge = order.index("CHANGE_STATUS_BRIDGE")
        meta_guard = order.index("META_GUARD")
        capability = order.index("_maybe_handle_capability_route")
        self.assertLess(task_intent, bridge, "bridge must run after task_intent")
        self.assertLess(bridge, meta_guard, "bridge must run before the meta-introspection guard")
        self.assertLess(meta_guard, capability, "guard must run before capability_route")

    def test_bridge_dispatches_exactly_once(self) -> None:
        # A duplicated bridge call would double the fall-through decision in
        # the consolidated dispatch_decision entry; first-occurrence dedup in
        # _ordered_markers would hide it, so count the raw sequence.
        sequence = _marker_sequence()
        self.assertEqual(
            sequence.count("CHANGE_STATUS_BRIDGE"),
            1,
            "the change_status bridge must be dispatched exactly once per turn",
        )

    def test_no_direct_handler_call_remains_in_handle_text_body(self) -> None:
        order = _ordered_markers()
        self.assertNotIn(
            "_maybe_handle_change_status_question",
            order,
            "change_status must be registry-invoked, not called directly",
        )

    def test_bridge_is_not_in_the_early_registry(self) -> None:
        # Joining _pre_brain_routes would move execution to the early slot
        # (rows 2-3) and reorder interception/telemetry — locked out here.
        src = inspect.getsource(BotService._build_pre_brain_routes)
        self.assertNotIn("change_status", src)


class RouteDataTests(unittest.TestCase):
    def test_slot_is_a_single_route_named_from_the_matcher(self) -> None:
        stub = SimpleNamespace(_route_change_status_question=lambda ctx: None)
        # Reproduce the slot construction contract from __init__ verbatim.
        slot = (Route(CHANGE_STATUS_MATCHER.name, stub._route_change_status_question),)
        init_src = inspect.getsource(BotService.__init__)
        self.assertIn(
            "Route(CHANGE_STATUS_MATCHER.name, self._route_change_status_question)", init_src
        )
        self.assertIn("_change_status_slot", init_src)
        self.assertEqual(len(slot), 1)
        self.assertEqual(slot[0].name, "change_status_question")

    def test_real_slot_assignment_is_a_one_route_tuple(self) -> None:
        # AST-verify the REAL assignment in __init__ (not a test-local
        # fixture): self._change_status_slot = (Route(...),) — exactly one
        # element, and that element is a Route(...) call.
        init_src = textwrap.dedent(inspect.getsource(BotService.__init__))
        tree = ast.parse(init_src)
        assignments = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(t, ast.Attribute) and t.attr == "_change_status_slot"
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
        ]
        self.assertEqual(len(assignments), 1, "_change_status_slot must be assigned exactly once")
        value = assignments[0].value
        self.assertIsInstance(value, ast.Tuple, "slot must be a tuple")
        self.assertEqual(len(value.elts), 1, "slot must hold exactly one Route")
        route_call = value.elts[0]
        self.assertIsInstance(route_call, ast.Call)
        self.assertIsInstance(route_call.func, ast.Name)
        self.assertEqual(route_call.func.id, "Route")


class PostCaptureContractTests(unittest.TestCase):
    # B4.5c is the no-guard variant (B4.5a shape): the legacy call site did
    # store(2000) then remember, with NO _quality_guard_response. These rails
    # freeze that contract at both the call site and the helper.

    def _bridge_block(self) -> str:
        body_src = inspect.getsource(BotService._handle_text_body)
        start = body_src.index("change_status_outcome = dispatch_routes")
        end = body_src.index("return change_status_outcome.response")
        return body_src[start:end]

    def test_call_site_post_captures_via_helper_with_outcome_limit(self) -> None:
        block = self._bridge_block()
        self.assertIn("self._post_capture_intercepted(", block)
        self.assertIn("assistant_limit=change_status_outcome.store_memory_limit", block)
        self.assertIn("on_decision=self._emit_route_decision", block)

    def test_no_guard_variant_call_site_and_adapter_never_quality_guard(self) -> None:
        self.assertNotIn("_quality_guard_response", self._bridge_block())
        adapter_src = inspect.getsource(BotService._route_change_status_question)
        self.assertNotIn("_quality_guard_response", adapter_src)

    def test_post_capture_helper_stores_then_remembers_with_exact_args(self) -> None:
        stub = SimpleNamespace()
        manager = MagicMock()
        stub._store_memory_turn = manager.store
        stub._remember_assistant_turn_state = manager.remember
        BotService._post_capture_intercepted.__get__(stub)(
            "s1", "estado de los cambios", "respuesta", assistant_limit=2000
        )
        self.assertEqual(
            manager.mock_calls,
            [
                call.store("s1", "estado de los cambios", "respuesta", assistant_limit=2000),
                call.remember("s1", "estado de los cambios", "respuesta"),
            ],
        )


def _make_stub() -> SimpleNamespace:
    """Minimal object carrying the real gate+renderer+adapter, with the
    response side's dependencies mocked (ledger empty + no recent commits →
    the renderer's deterministic no-data sentence)."""
    stub = SimpleNamespace()
    stub.task_ledger = None
    stub._recent_workspace_commits = MagicMock(return_value=[])
    stub._maybe_handle_change_status_question = (
        BotService._maybe_handle_change_status_question.__get__(stub)
    )
    stub._change_status_question_response = BotService._change_status_question_response.__get__(
        stub
    )
    stub._route_change_status_question = BotService._route_change_status_question.__get__(stub)
    stub._emit_dispatch_decision = MagicMock()
    stub._emit_route_decision = BotService._emit_route_decision.__get__(stub)
    return stub


def _ctx(text: str) -> RouteContext:
    return RouteContext(user_id="u1", session_id="s1", text=text, stripped=text.strip())


def _slot(stub: SimpleNamespace) -> tuple[Route, ...]:
    return (Route(CHANGE_STATUS_MATCHER.name, stub._route_change_status_question),)


class RegistryPathBehaviorTests(unittest.TestCase):
    # Corpus anchors from tests/test_b44a_declarative_matcher_pilot.py:
    # REPRESENTATIVE_DECISIONS positives/negatives plus the PR #246
    # estados-plural widening (live autocorrect incident).

    def test_positive_smoke_phrases_intercept_through_registry(self) -> None:
        for phrase in (
            "estado de los cambios",
            "Estatus de los fixes",
            "estátus de los cámbios",
        ):
            with self.subTest(phrase=phrase):
                stub = _make_stub()
                outcome = dispatch_routes(
                    _slot(stub), _ctx(phrase), on_decision=stub._emit_route_decision
                )
                self.assertEqual(outcome.route, "intercepted")
                self.assertTrue(outcome.captured)
                self.assertIsInstance(outcome.response, str)
                self.assertEqual(outcome.reason, "change_status_phrase_matched")
                self.assertEqual(outcome.store_memory_limit, 2000)
                kwargs = stub._emit_dispatch_decision.call_args.kwargs
                self.assertEqual(
                    kwargs,
                    {
                        "handler": "change_status_question",
                        "route": "intercepted",
                        "reason": "change_status_phrase_matched",
                        "session_id": "s1",
                        "text": phrase,
                        "captured": True,
                    },
                )

    def test_estados_plural_widening_intercepts_through_registry(self) -> None:
        # PR #246 deliberate widening (autocorrect plural). Must keep working
        # identically once the route is registry-invoked.
        stub = _make_stub()
        outcome = dispatch_routes(
            _slot(stub), _ctx("Estados de los cambios"), on_decision=stub._emit_route_decision
        )
        self.assertEqual(outcome.route, "intercepted")
        self.assertEqual(outcome.reason, "change_status_phrase_matched")

    def test_negative_smoke_phrases_fall_through_registry(self) -> None:
        for phrase in (
            "dame el estatus de los cambios",
            "Estatus de los fixes\ny de paso reinicia",
        ):
            with self.subTest(phrase=phrase):
                stub = _make_stub()
                outcome = dispatch_routes(
                    _slot(stub), _ctx(phrase), on_decision=stub._emit_route_decision
                )
                self.assertEqual(outcome.captured, False)
                self.assertIsNone(outcome.response)
                kwargs = stub._emit_dispatch_decision.call_args.kwargs
                self.assertEqual(
                    kwargs,
                    {
                        "handler": "change_status_question",
                        "route": "fall_through",
                        "reason": "change_status_phrase_no_match",
                        "session_id": "s1",
                        "text": phrase.strip(),
                        "captured": False,
                    },
                )

    def test_overlap_phrase_owned_by_operational_status_falls_through(self) -> None:
        # Overlap probe (mirror of the PR #236/B4.5b case): "hola estado de
        # los cambios" belongs to operational_status (greeting branch, an
        # EARLIER slot — §5.1 row 6). Through the chain it never reaches this
        # bridge; and even probed directly, the fullmatch contract must fall
        # through so ownership stays sum(owners) <= 1.
        stub = _make_stub()
        outcome = dispatch_routes(
            _slot(stub), _ctx("hola estado de los cambios"), on_decision=stub._emit_route_decision
        )
        self.assertEqual(outcome.route, "fall_through")
        self.assertFalse(outcome.captured)
        kwargs = stub._emit_dispatch_decision.call_args.kwargs
        self.assertEqual(kwargs["reason"], "change_status_phrase_no_match")
        self.assertEqual(kwargs["route"], "fall_through")

    def test_adapter_reason_slugs_come_from_the_matcher(self) -> None:
        src = inspect.getsource(BotService._route_change_status_question)
        self.assertIn("CHANGE_STATUS_MATCHER.unmatched_reason", src)
        self.assertIn("CHANGE_STATUS_MATCHER.matched_reason", src)
        self.assertNotIn('"change_status_phrase_matched"', src)
        self.assertNotIn('"change_status_phrase_no_match"', src)


if __name__ == "__main__":
    unittest.main()
