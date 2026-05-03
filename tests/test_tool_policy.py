from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.tool_policy import (
    DAEMON_AUTO_APPROVE,
    SECRET_PATH_PATTERNS,
    TOOL_POLICIES,
    ToolPolicy,
    daemon_can_auto_approve,
    path_is_secret,
    policy_for,
    risk_at_least,
    validate_workspace_path,
)


class RiskOrderTests(unittest.TestCase):
    def test_risk_at_least(self) -> None:
        self.assertTrue(risk_at_least("critical", "high"))
        self.assertTrue(risk_at_least("high", "high"))
        self.assertFalse(risk_at_least("low", "medium"))


class DaemonAutoApproveTests(unittest.TestCase):
    def test_safe_read_only_tools_can_auto_approve(self) -> None:
        for name in (
            "memory.read",
            "wiki.search",
            "task_ledger.read",
            "git.status",
            "observe.recent_events_redacted",
            "file.read_workspace_nonsecret",
        ):
            with self.subTest(name=name):
                self.assertTrue(daemon_can_auto_approve(name))

    def test_writes_cannot_auto_approve(self) -> None:
        for name in ("file.write", "Write", "Edit", "Bash"):
            with self.subTest(name=name):
                self.assertFalse(daemon_can_auto_approve(name))

    def test_critical_tools_cannot_auto_approve(self) -> None:
        for name in (
            "social.publish",
            "pipeline.merge",
            "deploy.production",
            "file.delete",
            "git.force_push",
        ):
            with self.subTest(name=name):
                self.assertFalse(daemon_can_auto_approve(name))

    def test_generic_file_read_not_in_allowlist(self) -> None:
        self.assertFalse(daemon_can_auto_approve("file.read"))
        self.assertFalse(daemon_can_auto_approve("Read"))
        self.assertNotIn("file.read", DAEMON_AUTO_APPROVE)
        self.assertNotIn("Read", DAEMON_AUTO_APPROVE)

    def test_unknown_tool_not_auto_approved(self) -> None:
        self.assertFalse(daemon_can_auto_approve("unknown.tool"))


class CriticalToolsRequireHumanTests(unittest.TestCase):
    def test_critical_tools_require_human(self) -> None:
        for name in (
            "social.publish",
            "pipeline.merge",
            "deploy.production",
            "file.delete",
            "git.force_push",
            "WikiDelete",
            "A2ASend",
            "SkillExecute",
        ):
            with self.subTest(name=name):
                policy = policy_for(name)
                self.assertTrue(policy.requires_human, msg=name)
                self.assertIn(policy.risk_level, {"high", "critical"})


class SecretPathTests(unittest.TestCase):
    def test_secret_paths_detected(self) -> None:
        for path in (
            ".env",
            ".env.local",
            "config/secrets.json",
            "id_rsa.pem",
            "data/credentials.txt",
            "browser_profile/cookies",
            "approvals/abc123.json",
            "vault.token",
        ):
            with self.subTest(path=path):
                self.assertTrue(path_is_secret(path), msg=path)

    def test_normal_paths_not_secret(self) -> None:
        for path in ("README.md", "src/main.py", "docs/intro.md"):
            with self.subTest(path=path):
                self.assertFalse(path_is_secret(path))


class WorkspacePathValidationTests(unittest.TestCase):
    def test_valid_path_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            (ws / "src").mkdir()
            target = ws / "src" / "main.py"
            target.write_text("ok")
            resolved = validate_workspace_path("src/main.py", workspace_root=ws)
            self.assertEqual(resolved, target.resolve())

    def test_path_outside_workspace_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir) / "ws"
            ws.mkdir()
            with self.assertRaises(PermissionError):
                validate_workspace_path("/etc/passwd", workspace_root=ws)

    def test_secret_path_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            (ws / ".env").write_text("SECRET=1")
            with self.assertRaises(PermissionError):
                validate_workspace_path(".env", workspace_root=ws)

    def test_encoded_secret_path_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            (ws / ".env").write_text("SECRET=1")
            with self.assertRaises(PermissionError):
                validate_workspace_path("%2eenv", workspace_root=ws)


class ToolPolicyDataclassTests(unittest.TestCase):
    def test_policy_for_returns_dataclass(self) -> None:
        policy = policy_for("memory.read")
        self.assertIsInstance(policy, ToolPolicy)
        self.assertEqual(policy.risk_level, "low")
        self.assertTrue(policy.read_only)
        self.assertIn("daemon", policy.allowed_contexts)

    def test_unknown_tool_returns_default(self) -> None:
        policy = policy_for("never.heard.of")
        self.assertEqual(policy.name, "<default>")
        self.assertFalse(policy.read_only)


class StrictToolSchemaTests(unittest.TestCase):
    def test_all_default_tool_schemas_are_strict(self) -> None:
        from claw_v2.tools import ToolRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            registry = ToolRegistry.default(workspace_root=tmpdir)
            schemas = registry.openai_tool_schemas()
            self.assertGreater(len(schemas), 0)
            for entry in schemas:
                params = entry["parameters"]
                if params.get("type") == "object":
                    self.assertEqual(
                        params.get("additionalProperties"),
                        False,
                        msg=f"{entry['name']} schema must set additionalProperties=false",
                    )

    def test_register_normalizes_added_schemas(self) -> None:
        from claw_v2.tools import ToolDefinition, ToolRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            registry = ToolRegistry(workspace_root=tmpdir)
            registry.register(
                ToolDefinition(
                    name="custom.tool",
                    description="custom",
                    allowed_agent_classes=("operator",),
                    handler=lambda args: {"ok": True},
                    parameter_schema={
                        "type": "object",
                        "properties": {
                            "config": {
                                "type": "object",
                                "properties": {"key": {"type": "string"}},
                            },
                        },
                    },
                    tier=1,
                )
            )
            defn = registry.get("custom.tool")
            self.assertEqual(defn.parameter_schema["additionalProperties"], False)
            self.assertEqual(
                defn.parameter_schema["properties"]["config"]["additionalProperties"],
                False,
            )


class WorkspaceReadHandlerTests(unittest.TestCase):
    def _registry(self, tmpdir: str):
        from claw_v2.tools import ToolRegistry
        return ToolRegistry.default(workspace_root=tmpdir)

    def _gate(self):
        def auto_gate(_definition, _args):
            return None
        return auto_gate

    def test_read_workspace_nonsecret_blocks_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".env").write_text("SECRET=1")
            registry = self._registry(tmpdir)
            with self.assertRaises(PermissionError):
                registry.execute(
                    "file.read_workspace_nonsecret",
                    {"path": ".env"},
                    agent_class="operator",
                    approval_gate=self._gate(),
                )

    def test_read_workspace_nonsecret_blocks_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._registry(tmpdir)
            with self.assertRaises(PermissionError):
                registry.execute(
                    "file.read_workspace_nonsecret",
                    {"path": "../../etc/passwd"},
                    agent_class="operator",
                    approval_gate=self._gate(),
                )

    def test_read_workspace_nonsecret_blocks_pem(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "id_rsa.pem").write_text("KEY")
            registry = self._registry(tmpdir)
            with self.assertRaises(PermissionError):
                registry.execute(
                    "file.read_workspace_nonsecret",
                    {"path": "id_rsa.pem"},
                    agent_class="operator",
                    approval_gate=self._gate(),
                )

    def test_read_workspace_nonsecret_allows_normal_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "src").mkdir()
            (Path(tmpdir) / "src" / "main.py").write_text("print('hi')")
            registry = self._registry(tmpdir)
            result = registry.execute(
                "file.read_workspace_nonsecret",
                {"path": "src/main.py"},
                agent_class="operator",
                approval_gate=self._gate(),
            )
            self.assertIn("print", result["content"])


class DaemonExecutorIntegrationTests(unittest.TestCase):
    def test_daemon_does_not_auto_approve_tier3_tool(self) -> None:
        import os
        from unittest.mock import patch
        from claw_v2.approval_gate import system_approval_mode, ApprovalPending
        from claw_v2.main import build_runtime
        from claw_v2.adapters.base import LLMResponse, LLMRequest

        def fake_anthropic(req: LLMRequest) -> LLMResponse:
            return LLMResponse(content="<response>ok</response>", lane=req.lane, provider="anthropic", model=req.model)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)

                # Call the openai_tool_executor closure indirectly: register a
                # high-risk tool name not in the daemon allowlist and confirm
                # the gate raises ApprovalPending instead of auto-approving.
                from claw_v2.tools import ToolDefinition
                handler_calls = []

                def handler(args):
                    handler_calls.append(args)
                    return {"ok": True}

                runtime.tool_registry.register(
                    ToolDefinition(
                        name="file.delete",
                        description="delete a workspace file",
                        allowed_agent_classes=("operator",),
                        handler=handler,
                        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
                        tier=3,
                    )
                )

                executor = runtime.openai_tool_executor
                with system_approval_mode(reason="scheduled_self_improve"):
                    with self.assertRaises(ApprovalPending):
                        executor("file.delete", {})

                self.assertEqual(handler_calls, [])

    def test_daemon_auto_approves_safe_read_only_tool(self) -> None:
        import os
        from unittest.mock import patch
        from claw_v2.approval_gate import system_approval_mode
        from claw_v2.main import build_runtime
        from claw_v2.adapters.base import LLMResponse, LLMRequest

        def fake_anthropic(req: LLMRequest) -> LLMResponse:
            return LLMResponse(content="<response>ok</response>", lane=req.lane, provider="anthropic", model=req.model)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)

                from claw_v2.tools import ToolDefinition

                def handler(args):
                    return {"events": []}

                runtime.tool_registry.register(
                    ToolDefinition(
                        name="memory.read",
                        description="read memory rows",
                        allowed_agent_classes=("operator",),
                        handler=handler,
                        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
                        tier=1,
                    )
                )

                executor = runtime.openai_tool_executor
                with system_approval_mode(reason="scheduled_heartbeat"):
                    result = executor("memory.read", {})

                self.assertEqual(result, {"events": []})


if __name__ == "__main__":
    unittest.main()
