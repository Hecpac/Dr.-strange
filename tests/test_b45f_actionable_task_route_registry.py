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
from claw_v2.dispatch.matchers import ACTIONABLE_TASK_MATCHER

# B4.5f moves only the _handle_text_body invocation of the actionable-task
# handler into a one-Route per-slot registry bridge at the ORIGINAL row 8:
# owner_delegation < telegram imperative < actionable bridge < F4. Runtime,
# slash, session/follow-up/continuation, preflight and execution semantics stay
# inside the unchanged legacy handler. The already-classified semantic_turn is
# transported through RouteContext.metadata. This is the B4.5e no-guard shape,
# while preserving actionables' exact decision -> store -> remember -> return
# capture path and 2000-character memory limit.


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
    "_actionable_task_slot": "ACTIONABLE_TASK_BRIDGE",
}


def _marker_sequence() -> list[str]:
    markers: list[tuple[int, int, str]] = []
    for statement in _handle_text_body_ast().body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in [statement, *list(_walk_excluding_closures(statement))]:
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                markers.append((node.lineno, node.col_offset, node.func.attr))
            elif isinstance(node.func, ast.Name):
                name = node.func.id
                if name == "dispatch_routes" and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Attribute) and first.attr in _BRIDGE_MARKERS:
                        name = _BRIDGE_MARKERS[first.attr]
                markers.append((node.lineno, node.col_offset, name))
    markers.sort()
    return [name for _, _, name in markers]


def _ordered_markers() -> list[str]:
    seen: list[str] = []
    for name in _marker_sequence():
        if name not in seen:
            seen.append(name)
    return seen


class BridgeSlotPositionTests(unittest.TestCase):
    def test_exact_order_owner_then_imperative_then_actionable_then_f4(self) -> None:
        order = _ordered_markers()
        self.assertIn("OWNER_DELEGATION_BRIDGE", order)
        self.assertIn("ACTIONABLE_TASK_BRIDGE", order, "actionable bridge not found")
        owner = order.index("OWNER_DELEGATION_BRIDGE")
        imperative = order.index("_maybe_handle_telegram_imperative_request")
        actionable = order.index("ACTIONABLE_TASK_BRIDGE")
        f4 = order.index("_maybe_handle_f4_deterministic_delegation")
        self.assertLess(owner, imperative)
        self.assertLess(imperative, actionable)
        self.assertLess(actionable, f4)

    def test_actionable_bridge_dispatches_exactly_once(self) -> None:
        self.assertEqual(_marker_sequence().count("ACTIONABLE_TASK_BRIDGE"), 1)

    def test_no_direct_actionable_handler_call_remains_in_text_chain(self) -> None:
        self.assertNotIn(
            "_maybe_handle_actionable_task_request",
            _ordered_markers(),
            "actionable task must be registry-invoked in _handle_text_body",
        )

    def test_bridge_call_has_exact_registry_callback_shape(self) -> None:
        candidates = [
            node
            for node in ast.walk(_handle_text_body_ast())
            if isinstance(node, ast.Call)
            and _call_path(node) == "dispatch_routes"
            and node.args
            and ast.dump(node.args[0], include_attributes=False)
            == _expression_dump("self._actionable_task_slot")
        ]
        self.assertEqual(len(candidates), 1)
        call_node = candidates[0]
        self.assertEqual(
            [ast.dump(arg, include_attributes=False) for arg in call_node.args],
            [
                _expression_dump("self._actionable_task_slot"),
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
            "actionable_task",
            inspect.getsource(BotService._build_pre_brain_routes),
        )


class RouteDataTests(unittest.TestCase):
    def test_real_slot_is_one_route_named_from_matcher(self) -> None:
        tree = ast.parse(textwrap.dedent(inspect.getsource(BotService.__init__)))
        assignments = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Attribute) and target.attr == "_actionable_task_slot"
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
        self.assertEqual(
            [ast.dump(arg, include_attributes=False) for arg in route_call.args],
            [
                _expression_dump("ACTIONABLE_TASK_MATCHER.name"),
                _expression_dump("self._route_actionable_task"),
            ],
        )


def _ctx(
    text: str,
    *,
    runtime_channel: str | None = "telegram",
    semantic_turn: object | None = None,
) -> RouteContext:
    return RouteContext(
        user_id="u1",
        session_id="s1",
        text=text,
        stripped=text.strip(),
        runtime_channel=runtime_channel,
        metadata={"semantic_turn": semantic_turn},
    )


def _make_stub(response: str | None) -> SimpleNamespace:
    stub = SimpleNamespace(
        _maybe_handle_actionable_task_request=MagicMock(return_value=response),
        _emit_dispatch_decision=MagicMock(),
    )
    stub._route_actionable_task = BotService._route_actionable_task.__get__(stub)
    stub._emit_route_decision = BotService._emit_route_decision.__get__(stub)
    return stub


def _slot(stub: SimpleNamespace) -> tuple[Route, ...]:
    return (Route(ACTIONABLE_TASK_MATCHER.name, stub._route_actionable_task),)


class RegistryPathTests(unittest.TestCase):
    def test_none_falls_through_with_static_unmatched_decision(self) -> None:
        semantic_turn = object()
        stub = _make_stub(None)
        outcome = dispatch_routes(
            _slot(stub),
            _ctx("hola", semantic_turn=semantic_turn),
            on_decision=stub._emit_route_decision,
        )
        self.assertEqual(outcome.route, "fall_through")
        self.assertFalse(outcome.captured)
        self.assertIsNone(outcome.response)
        self.assertEqual(outcome.reason, "no_route_matched")
        self.assertEqual(
            stub._emit_dispatch_decision.call_args_list,
            [
                call(
                    handler=ACTIONABLE_TASK_MATCHER.name,
                    route="fall_through",
                    reason=ACTIONABLE_TASK_MATCHER.unmatched_reason,
                    session_id="s1",
                    text="hola",
                    captured=False,
                )
            ],
        )

    def test_capture_intercepts_with_static_matched_decision_and_2000_limit(self) -> None:
        semantic_turn = object()
        stub = _make_stub("captured")
        outcome = dispatch_routes(
            _slot(stub),
            _ctx("termina el PR", semantic_turn=semantic_turn),
            on_decision=stub._emit_route_decision,
        )
        self.assertEqual(outcome.route, "intercepted")
        self.assertTrue(outcome.captured)
        self.assertEqual(outcome.response, "captured")
        self.assertEqual(outcome.reason, ACTIONABLE_TASK_MATCHER.matched_reason)
        self.assertEqual(outcome.store_memory_limit, 2000)
        self.assertEqual(
            stub._emit_dispatch_decision.call_args_list,
            [
                call(
                    handler=ACTIONABLE_TASK_MATCHER.name,
                    route="intercepted",
                    reason=ACTIONABLE_TASK_MATCHER.matched_reason,
                    session_id="s1",
                    text="termina el PR",
                    captured=True,
                )
            ],
        )

    def test_adapter_passes_exact_legacy_arguments_and_semantic_identity(self) -> None:
        semantic_turn = object()
        stub = _make_stub(None)
        outcome = stub._route_actionable_task(
            _ctx("  termina el PR  ", semantic_turn=semantic_turn)
        )
        stub._maybe_handle_actionable_task_request.assert_called_once_with(
            "termina el PR",
            session_id="s1",
            runtime_channel="telegram",
            semantic_turn=semantic_turn,
        )
        self.assertEqual(outcome.route, "fall_through")
        self.assertEqual(outcome.reason, ACTIONABLE_TASK_MATCHER.unmatched_reason)


class AdapterAndCallSiteTests(unittest.TestCase):
    def test_route_context_transports_semantic_turn_in_metadata(self) -> None:
        body = _function_ast(BotService._handle_text_body)
        route_context_calls = [
            node
            for node in ast.walk(body)
            if isinstance(node, ast.Call) and _call_path(node) == "RouteContext"
        ]
        self.assertEqual(len(route_context_calls), 1)
        metadata = next(
            keyword.value
            for keyword in route_context_calls[0].keywords
            if keyword.arg == "metadata"
        )
        self.assertEqual(
            ast.dump(metadata, include_attributes=False),
            _expression_dump('{"semantic_turn": semantic_turn}'),
        )

    def test_adapter_is_pure_and_slugs_come_only_from_matcher(self) -> None:
        src = inspect.getsource(BotService._route_actionable_task)
        self.assertIn('ctx.metadata["semantic_turn"]', src)
        self.assertIn("ACTIONABLE_TASK_MATCHER.unmatched_reason", src)
        self.assertIn("ACTIONABLE_TASK_MATCHER.matched_reason", src)
        self.assertNotIn('"telegram_actionable_task_matched"', src)
        self.assertNotIn('"telegram_actionable_task_no_match"', src)
        for forbidden in (
            "_emit_dispatch_decision",
            "_quality_guard_response",
            "_final_render",
            "_post_capture_intercepted",
            "matched_pattern",
        ):
            self.assertNotIn(forbidden, src)
        adapter = _function_ast(BotService._route_actionable_task)
        call_paths = {_call_path(node) for node in ast.walk(adapter) if isinstance(node, ast.Call)}
        self.assertNotIn("ACTIONABLE_TASK_MATCHER.match", call_paths)
        self.assertFalse(any(isinstance(node, ast.JoinedStr) for node in ast.walk(adapter)))

    def test_capture_path_is_decision_then_store_remember_return_without_guard(self) -> None:
        body = _function_ast(BotService._handle_text_body)
        body_src = inspect.getsource(BotService._handle_text_body)
        bridge_at = body_src.index("actionable_task_outcome = dispatch_routes")
        store_at = body_src.index("self._store_memory_turn", bridge_at)
        remember_at = body_src.index("self._remember_assistant_turn_state", store_at)
        return_at = body_src.index("return actionable_task_outcome.response", remember_at)
        f4_at = body_src.index("f4_delegation_response =", return_at)
        self.assertLess(bridge_at, store_at)
        self.assertLess(store_at, remember_at)
        self.assertLess(remember_at, return_at)
        self.assertLess(return_at, f4_at)

        actionable_block = body_src[bridge_at:f4_at]
        self.assertIn("on_decision=self._emit_route_decision", actionable_block)
        self.assertIn(
            "assistant_limit=actionable_task_outcome.store_memory_limit",
            actionable_block,
        )
        for forbidden in (
            "_quality_guard_response",
            "_final_render",
            "_post_capture_intercepted",
            "matched_pattern",
            "_maybe_handle_actionable_task_request",
        ):
            self.assertNotIn(forbidden, actionable_block)

        capture_test = _expression_dump(
            "actionable_task_outcome.captured and actionable_task_outcome.response is not None"
        )
        capture_ifs = [
            statement
            for statement in body.body
            if isinstance(statement, ast.If)
            and ast.dump(statement.test, include_attributes=False) == capture_test
        ]
        self.assertEqual(len(capture_ifs), 1)
        capture_if = capture_ifs[0]
        self.assertEqual(capture_if.orelse, [])
        self.assertEqual(len(capture_if.body), 3)
        self.assertEqual(
            [_call_path(statement.value) for statement in capture_if.body[:2]],
            ["self._store_memory_turn", "self._remember_assistant_turn_state"],
        )
        self.assertIsInstance(capture_if.body[2], ast.Return)


if __name__ == "__main__":
    unittest.main()
