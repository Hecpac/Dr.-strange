from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from claw_v2.bot import BotService

# B4.1/B4.2 — BotService migration rails (2026-07-07, hardened per PR #232
# review). Preconditions for any strangler/migration work on BotService: the
# pre-brain dispatch order is behavior (WIRING §5.1: "Order matters"), and the
# god-module must stop growing while it awaits migration. Changing either is a
# DELIBERATE, test-visible edit — never a side effect.

# FULL top-level dispatch/capture sequence in BotService._handle_text_body,
# extracted via AST (source order, closures excluded — the method defines
# local helpers whose bodies would otherwise pollute the flow order). Scope
# precision (PR #232 review): NLM/wiki dispatch is delegated to NlmHandler
# inside the _maybe_handle_shortcut subtree — this rail locks the shortcut
# call's top-level position, NOT the NLM-internal order.
EXPECTED_PRE_BRAIN_ORDER = [
    "_maybe_handle_brain_first_new_task",
    "_handle_pending_computer_approval_response",
    "_handle_pending_tasks_query",
    "_maybe_handle_operational_failure_summary",
    # B4.5b: operational_status migrated to the route registry via a per-slot
    # bridge (dispatch_routes(self._operational_status_slot, ...)) at this
    # exact position — between operational_failure_summary and the B4.5a
    # cleanup bridge. Locked by
    # tests/test_b45b_operational_status_route_registry.py.
    # B4.5a: cleanup_status migrated to the route registry via a per-slot
    # bridge (dispatch_routes(self._cleanup_status_slot, ...)) that runs at
    # this exact position — between the operational_status bridge and
    # owner_delegation. The AST extractor only sees _maybe_handle_*/_handle_*
    # names, so the registry-invoked slots are locked by
    # tests/test_b45a_cleanup_route_registry.py and the b45b test instead.
    "_maybe_handle_owner_delegation_request",
    "_maybe_handle_telegram_imperative_request",
    "_handle_stateful_brain_shortcut",
    "_maybe_handle_actionable_task_request",
    "_maybe_handle_f4_deterministic_delegation",
    "_maybe_handle_task_intent",
    "_maybe_handle_change_status_question",
    "_maybe_handle_capability_route",
    "_handle_pending_tool_approval_grant_response",
    "_handle_autonomy_grant_response",
    "_maybe_resolve_stateful_followup",
    "_maybe_handle_shortcut",
    "maybe_run_coordinated_task",
]

# B4.2 size-ratchet: baseline measured at merge 634a528 (12172 lines) plus an
# explicit tiny allowance for surgical fixes. Growing past this means new code
# is landing in the god-module instead of an extracted home — move it, or
# raise the baseline HERE, deliberately, with review.
BOTSERVICE_LINE_BASELINE = 12172
BOTSERVICE_LINE_ALLOWANCE = 150


def _botservice_source_path() -> Path:
    source_file = inspect.getsourcefile(BotService)
    assert source_file is not None, "BotService source file not resolvable"
    return Path(source_file)


def _is_dispatch_call_name(name: str) -> bool:
    # Capture/dispatch handlers only — bookkeeping (_remember_*, _emit_*,
    # _flush_*, guards, renderers) stays out by prefix. Any receiver counts
    # (maybe_run_coordinated_task is called on the task handler, not self).
    return (
        name.startswith(("_maybe_handle_", "_maybe_resolve_", "_handle_"))
        or name == "maybe_run_coordinated_task"
    )


def _walk_excluding_closures(node: ast.AST):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield child
        yield from _walk_excluding_closures(child)


def _top_level_handler_order() -> list[str]:
    """AST-based (PR #232 review): parse the real source file, take
    _handle_text_body's top-level statements (nested helper defs excluded),
    and return dispatch-call names in true source order, first occurrence."""
    source = _botservice_source_path().read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_handle_text_body"
    )
    calls: list[tuple[int, int, str]] = []
    for stmt in fn.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in (stmt, *_walk_excluding_closures(stmt)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                calls.append((node.lineno, node.col_offset, node.func.attr))
    calls.sort()
    ordered: list[str] = []
    for _, _, name in calls:
        if _is_dispatch_call_name(name) and name not in ordered:
            ordered.append(name)
    return ordered


class PreBrainOrderLockTests(unittest.TestCase):
    def test_pre_brain_dispatch_order_is_locked(self) -> None:
        self.assertEqual(
            _top_level_handler_order(),
            EXPECTED_PRE_BRAIN_ORDER,
            "Pre-brain dispatch order changed. WIRING §5.1: order IS behavior. "
            "If this reorder/insertion is intentional (e.g. a migration step), "
            "update EXPECTED_PRE_BRAIN_ORDER and §5.1 in the same commit.",
        )

    def test_representative_ordering_decisions_hold(self) -> None:
        order = _top_level_handler_order()
        idx = {name: i for i, name in enumerate(order)}
        # Brain-first semantic routing outruns every literal matcher.
        self.assertEqual(idx["_maybe_handle_brain_first_new_task"], 0)
        # A pending computer approval must be resolved before any imperative
        # matcher can steal the turn (grant words look like imperatives).
        self.assertLess(
            idx["_handle_pending_computer_approval_response"],
            idx["_maybe_handle_telegram_imperative_request"],
        )
        # Pending-tasks queries capture before the operational group.
        self.assertLess(
            idx["_handle_pending_tasks_query"],
            idx["_maybe_handle_operational_failure_summary"],
        )
        # F4-B1 deterministic delegation captures BEFORE the broad task-intent
        # router (exactly-once contract on the message id).
        self.assertLess(
            idx["_maybe_handle_f4_deterministic_delegation"],
            idx["_maybe_handle_task_intent"],
        )
        # Approval/autonomy grant resolution precedes the stateful followup
        # resolver, and both precede the generic shortcut.
        self.assertLess(
            idx["_handle_pending_tool_approval_grant_response"],
            idx["_maybe_resolve_stateful_followup"],
        )
        self.assertLess(
            idx["_handle_autonomy_grant_response"],
            idx["_maybe_resolve_stateful_followup"],
        )
        self.assertLess(idx["_maybe_resolve_stateful_followup"], idx["_maybe_handle_shortcut"])
        # The generic shortcut is the last _maybe_handle_* capture; the
        # autonomous coordinated-task path is the FINAL capture before the
        # brain default.
        self.assertEqual(
            [n for n in order if n.startswith("_maybe_handle_")][-1],
            "_maybe_handle_shortcut",
        )
        self.assertEqual(order[-1], "maybe_run_coordinated_task")

    def test_every_locked_self_handler_still_exists(self) -> None:
        for name in EXPECTED_PRE_BRAIN_ORDER:
            if name == "maybe_run_coordinated_task":
                continue  # lives on TaskHandler, asserted by the order lock
            self.assertTrue(hasattr(BotService, name), name)


class BotServiceSizeRatchetTests(unittest.TestCase):
    def test_botservice_module_does_not_grow(self) -> None:
        lines = len(_botservice_source_path().read_text(encoding="utf-8").splitlines())
        limit = BOTSERVICE_LINE_BASELINE + BOTSERVICE_LINE_ALLOWANCE
        self.assertLessEqual(
            lines,
            limit,
            f"claw_v2/bot.py is {lines} lines (ratchet: {limit}). BotService "
            "awaits migration — new code goes in an extracted module, not the "
            "god-module. If growth here is genuinely unavoidable, raise "
            "BOTSERVICE_LINE_BASELINE deliberately in this file with review.",
        )

    def test_ratchet_allowance_stays_tiny(self) -> None:
        # The allowance exists for surgical fixes, not for a slow-motion leak.
        self.assertLessEqual(BOTSERVICE_LINE_ALLOWANCE, 150)


if __name__ == "__main__":
    unittest.main()
