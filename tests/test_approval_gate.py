from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.approval import ApprovalManager
from claw_v2.approval_gate import (
    ApprovalPending,
    build_system_auto_approve_gate,
    build_telegram_approval_gate,
)
from claw_v2.tools import (
    TIER_REQUIRES_APPROVAL,
    ToolDefinition,
    ToolRegistry,
)


class _FakeObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.events.append((event, kwargs))


class TelegramApprovalGateTests(unittest.TestCase):
    """Paso 4 acceptance criteria (HEC-14)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.approvals = ApprovalManager(self.root / "approvals", secret="test-secret")
        self.observe = _FakeObserve()
        self.registry = ToolRegistry.default(
            workspace_root=self.root / "workspace", memory=None
        )
        (self.root / "workspace").mkdir(parents=True, exist_ok=True)
        self.registry.observe = self.observe

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # Acceptance: Tier 1 keeps working without changes.
    def test_tier1_unchanged_with_gate_wired(self) -> None:
        target = self.root / "workspace" / "readme.txt"
        target.write_text("hello", encoding="utf-8")
        gate = build_telegram_approval_gate(self.approvals)
        result = self.registry.execute(
            "Read",
            {"path": str(target)},
            agent_class="researcher",
            approval_gate=gate,
        )
        self.assertEqual(result["content"], "hello")
        self.assertEqual(self.approvals.list_pending(), [])
        events = [e[0] for e in self.observe.events]
        self.assertIn("AUTONOMY_BYPASS", events)

    # Acceptance: Tier 3 from Telegram -> PendingApproval record, no PermissionError.
    def test_tier3_creates_pending_and_raises_approval_pending(self) -> None:
        calls: list[str] = []
        self.registry.register(
            ToolDefinition(
                name="SendMoney",
                description="tier3 test",
                parameter_schema={"type": "object", "properties": {}},
                handler=lambda _: calls.append("executed") or {"ok": True},
                allowed_agent_classes=("operator",),
                tier=TIER_REQUIRES_APPROVAL,
            )
        )
        notified: list[str] = []
        gate = build_telegram_approval_gate(
            self.approvals, notifier=lambda p: notified.append(p.approval_id)
        )

        with self.assertRaises(ApprovalPending) as ctx:
            self.registry.execute(
                "SendMoney",
                {"amount": 100},
                agent_class="operator",
                approval_gate=gate,
            )

        # Not a PermissionError — distinct exception the bot can surface.
        self.assertNotIsInstance(ctx.exception, PermissionError)
        self.assertEqual(ctx.exception.tool, "SendMoney")
        self.assertTrue(ctx.exception.approval_id)
        self.assertTrue(ctx.exception.token)

        # Handler MUST NOT run until approved.
        self.assertEqual(calls, [])

        # ApprovalManager has the pending record.
        pending = self.approvals.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["action"], "tool:SendMoney")
        self.assertEqual(pending[0]["metadata"]["tool"], "SendMoney")
        self.assertEqual(pending[0]["metadata"]["tier"], TIER_REQUIRES_APPROVAL)

        # Notifier was invoked with the same approval id.
        self.assertEqual(notified, [ctx.exception.approval_id])

    # Acceptance: approved token lets the retry succeed.
    def test_tier3_retry_after_approval_executes_handler(self) -> None:
        calls: list[dict] = []
        self.registry.register(
            ToolDefinition(
                name="Deploy",
                description="tier3 test",
                parameter_schema={"type": "object", "properties": {}},
                handler=lambda args: calls.append(args) or {"deployed": True},
                allowed_agent_classes=("operator",),
                tier=TIER_REQUIRES_APPROVAL,
            )
        )
        gate = build_telegram_approval_gate(self.approvals)
        with self.assertRaises(ApprovalPending) as ctx:
            self.registry.execute(
                "Deploy", {"env": "prod"}, agent_class="operator", approval_gate=gate
            )
        self.assertTrue(self.approvals.approve(ctx.exception.approval_id, ctx.exception.token))

        # Second attempt wires a pass-through gate that trusts the prior approval.
        def trust_gate(defn, args):  # type: ignore[no-untyped-def]
            return None

        result = self.registry.execute(
            "Deploy",
            {"env": "prod"},
            agent_class="operator",
            approval_gate=trust_gate,
        )
        self.assertEqual(result, {"deployed": True})
        self.assertEqual(calls, [{"env": "prod"}])


class SystemAutoApproveGateTests(unittest.TestCase):
    def test_daemon_gate_auto_approves_and_runs_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            approvals = ApprovalManager(root / "approvals", secret="test-secret")
            registry = ToolRegistry.default(workspace_root=root / "ws", memory=None)
            (root / "ws").mkdir(parents=True, exist_ok=True)
            ran: list[bool] = []
            registry.register(
                ToolDefinition(
                    name="CronJob",
                    description="tier3 autonomous",
                    parameter_schema={"type": "object", "properties": {}},
                    handler=lambda _: ran.append(True) or {"ok": True},
                    allowed_agent_classes=("operator",),
                    tier=TIER_REQUIRES_APPROVAL,
                )
            )
            gate = build_system_auto_approve_gate(approvals, reason="heartbeat")
            result = registry.execute(
                "CronJob", {}, agent_class="operator", approval_gate=gate
            )
            self.assertEqual(result, {"ok": True})
            self.assertEqual(ran, [True])
            # Approval record exists and is approved (not pending).
            self.assertEqual(approvals.list_pending(), [])
            records = list((root / "approvals").glob("*.json"))
            self.assertEqual(len(records), 1)


class BotFormatterTests(unittest.TestCase):
    """Point 1 of Last-Mile Sequence: BotService surfaces ApprovalPending as UX."""

    def test_format_approval_pending_contains_command(self) -> None:
        from claw_v2.bot import _format_approval_pending

        exc = ApprovalPending(
            approval_id="abc123",
            token="tok-xyz",
            tool="Deploy",
            summary="Deploy(env)",
        )
        msg = _format_approval_pending(exc)
        self.assertIn("Tier 3", msg)
        self.assertIn("Deploy", msg)
        self.assertIn("/approve abc123 tok-xyz", msg)
        # Must not expose a traceback or "error" language.
        self.assertNotIn("Traceback", msg)
        self.assertNotIn("PermissionError", msg)


class DaemonContextModeTests(unittest.TestCase):
    """Point 3: `system_approval_mode` flips the shared executor to auto-approve."""

    def test_context_var_picks_system_gate_for_daemon(self) -> None:
        from claw_v2.approval_gate import (
            build_system_auto_approve_gate,
            current_daemon_reason,
            system_approval_mode,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            approvals = ApprovalManager(root / "approvals", secret="s")
            registry = ToolRegistry.default(workspace_root=root / "ws", memory=None)
            (root / "ws").mkdir(parents=True, exist_ok=True)
            ran: list[bool] = []
            registry.register(
                ToolDefinition(
                    name="ScheduledTask",
                    description="tier3 daemon test",
                    parameter_schema={"type": "object", "properties": {}},
                    handler=lambda _: ran.append(True) or {"ok": True},
                    allowed_agent_classes=("operator",),
                    tier=TIER_REQUIRES_APPROVAL,
                )
            )
            telegram_gate = build_telegram_approval_gate(approvals)

            def shared_executor(name, args):  # same shape as main.py closure
                reason = current_daemon_reason()
                gate = (
                    build_system_auto_approve_gate(approvals, reason=reason)
                    if reason is not None
                    else telegram_gate
                )
                return registry.execute(
                    name, args, agent_class="operator", approval_gate=gate
                )

            # Without daemon mode -> ApprovalPending (Telegram path).
            self.assertIsNone(current_daemon_reason())
            with self.assertRaises(ApprovalPending):
                shared_executor("ScheduledTask", {})
            self.assertEqual(ran, [])

            # Inside daemon mode -> auto-approve, handler runs.
            with system_approval_mode(reason="Scheduled Kairos Tick"):
                self.assertEqual(current_daemon_reason(), "Scheduled Kairos Tick")
                result = shared_executor("ScheduledTask", {})
            self.assertEqual(result, {"ok": True})
            self.assertEqual(ran, [True])

            # Context cleared after exit.
            self.assertIsNone(current_daemon_reason())

            # Audit trail: one approved record exists with the daemon reason.
            records = [ApprovalManager(root / "approvals", secret="s").read(p.stem)
                       for p in sorted((root / "approvals").glob("*.json"))]
            approved = [r for r in records if r["status"] == "approved"]
            self.assertEqual(len(approved), 1)
            self.assertEqual(
                approved[0]["metadata"]["auto_approved_reason"],
                "Scheduled Kairos Tick",
            )


class GoldenPathTests(unittest.TestCase):
    """Paso 5: end-to-end Telegram → Tier 3 → /approve → approved → retry."""

    def test_telegram_tier3_round_trip(self) -> None:
        from claw_v2.adapters.openai import OpenAIAdapter
        from claw_v2.bot import _format_approval_pending

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            approvals = ApprovalManager(root / "approvals", secret="s")
            registry = ToolRegistry.default(workspace_root=root / "ws", memory=None)
            (root / "ws").mkdir(parents=True, exist_ok=True)
            executed_args: list[dict] = []
            registry.register(
                ToolDefinition(
                    name="DeploySite",
                    description="tier3 deploy",
                    parameter_schema={"type": "object", "properties": {}},
                    handler=lambda args: (
                        executed_args.append(args) or {"deployed": True}
                    ),
                    allowed_agent_classes=("operator",),
                    tier=TIER_REQUIRES_APPROVAL,
                )
            )
            gate = build_telegram_approval_gate(approvals)

            def shared_executor(name, args):
                return registry.execute(
                    name, args, agent_class="operator", approval_gate=gate
                )

            adapter = OpenAIAdapter(
                transport=None,
                tool_executor=shared_executor,
                tool_schemas=[{"name": "DeploySite"}],
            )

            class _Call:
                type = "function_call"
                name = "DeploySite"
                call_id = "c1"
                arguments = '{"env": "prod"}'

            class _Resp:
                id = "r1"
                output = [_Call()]

            # Step 1: brain triggers Tier 3 tool → adapter raises
            # ApprovalPending (propagates as proven in SubAgentPropagationTests).
            captured: list[ApprovalPending] = []
            try:
                adapter._tool_loop(client=object(), request=None, response=_Resp())
                self.fail("expected ApprovalPending")
            except ApprovalPending as e:
                captured.append(e)
                # Step 2: bot formats the response Hector will see in Telegram.
                reply = _format_approval_pending(e)

            exc = captured[0]
            # Step 3: handler has NOT run yet.
            self.assertEqual(executed_args, [])
            self.assertIn("/approve ", reply)
            self.assertIn(exc.approval_id, reply)
            self.assertIn(exc.token, reply)

            # Step 4: parse `/approve <id> <token>` from the reply the way the
            # bot command dispatcher does — exactly the surface the user types.
            tokens = reply.split("/approve ")[1].split()
            parsed_id = tokens[0].strip("`")
            parsed_token = tokens[1].strip("`")
            self.assertTrue(approvals.approve(parsed_id, parsed_token))
            self.assertEqual(approvals.status(parsed_id), "approved")

            # Step 5: after approval, a retry with a passthrough gate (mirrors
            # "already approved in this session" semantics) runs the handler.
            def trust_gate(defn, args):
                return None

            result = registry.execute(
                "DeploySite",
                {"env": "prod"},
                agent_class="operator",
                approval_gate=trust_gate,
            )
            self.assertEqual(result, {"deployed": True})
            self.assertEqual(executed_args, [{"env": "prod"}])


class SubAgentPropagationTests(unittest.TestCase):
    """Point 2: the approval_gate wired at Brain/main.py inherits down to
    sub-agents because they share the same openai_tool_executor closure through
    OpenAIAdapter. We simulate a sub-agent dispatch that triggers a Tier 3
    tool and verify ApprovalPending bubbles past the adapter boundary unchanged.
    """

    def test_openai_adapter_propagates_approval_pending(self) -> None:
        from claw_v2.adapters.openai import OpenAIAdapter

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            approvals = ApprovalManager(root / "approvals", secret="test-secret")
            registry = ToolRegistry.default(workspace_root=root / "ws", memory=None)
            (root / "ws").mkdir(parents=True, exist_ok=True)
            registry.register(
                ToolDefinition(
                    name="DeploySite",
                    description="tier3 test",
                    parameter_schema={"type": "object", "properties": {}},
                    handler=lambda _: {"deployed": True},
                    allowed_agent_classes=("operator",),
                    tier=TIER_REQUIRES_APPROVAL,
                )
            )
            gate = build_telegram_approval_gate(approvals)

            # Closure the Brain shares with sub-agents (same shape as main.py).
            def shared_tool_executor(name, args):  # type: ignore[no-untyped-def]
                return registry.execute(
                    name, args, agent_class="operator", approval_gate=gate
                )

            adapter = OpenAIAdapter(
                transport=None,
                tool_executor=shared_tool_executor,
                tool_schemas=[{"name": "DeploySite"}],
            )

            class _Call:
                type = "function_call"
                name = "DeploySite"
                call_id = "c1"
                arguments = "{}"

            class _Resp:
                id = "r1"
                output = [_Call()]

            # _tool_loop must not swallow ApprovalPending.
            with self.assertRaises(ApprovalPending) as ctx:
                adapter._tool_loop(client=object(), request=None, response=_Resp())
            self.assertEqual(ctx.exception.tool, "DeploySite")
            self.assertEqual(len(approvals.list_pending()), 1)


if __name__ == "__main__":
    unittest.main()
