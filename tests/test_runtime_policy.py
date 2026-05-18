from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.runtime_policy import RuntimePolicyEngine
from claw_v2.sandbox import SandboxPolicy
from claw_v2.tools import ToolDefinition, ToolRegistry


class RuntimePolicyEngineTests(unittest.TestCase):
    def test_unknown_tool_is_denied_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            engine = RuntimePolicyEngine(workspace_root=workspace, sandbox_policy=SandboxPolicy(workspace_root=workspace))

            with self.assertRaises(PermissionError) as ctx:
                engine.enforce("not.in.policy", {}, context="operator")

            self.assertIn("not declared", str(ctx.exception))

    def test_secret_paths_are_blocked_for_any_disk_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / ".env").write_text("SECRET=1", encoding="utf-8")
            engine = RuntimePolicyEngine(workspace_root=workspace, sandbox_policy=SandboxPolicy(workspace_root=workspace))

            with self.assertRaises(PermissionError):
                engine.enforce("Read", {"path": ".env"}, context="operator")

    def test_read_only_policy_rejects_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "README.md").write_text("ok", encoding="utf-8")
            engine = RuntimePolicyEngine(workspace_root=workspace, sandbox_policy=SandboxPolicy(workspace_root=workspace))

            with self.assertRaises(PermissionError) as ctx:
                engine.enforce("Read", {"path": "README.md"}, context="operator", mutates_state=True)

            self.assertIn("read-only", str(ctx.exception))

    def test_tool_registry_with_sandbox_denies_unlisted_registered_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            registry = ToolRegistry(workspace_root=workspace)
            registry.register(
                ToolDefinition(
                    name="UndeclaredTool",
                    description="not in tool_policies.json",
                    allowed_agent_classes=("operator",),
                    handler=lambda _args: {"ok": True},
                    tier=1,
                )
            )

            with self.assertRaises(PermissionError) as ctx:
                registry.execute(
                    "UndeclaredTool",
                    {},
                    agent_class="operator",
                    policy=SandboxPolicy(workspace_root=workspace),
                )

            self.assertIn("not declared", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
