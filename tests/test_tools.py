from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.memory import MemoryStore
from claw_v2.network_proxy import DomainAllowlistEnforcer
from claw_v2.sandbox import SandboxPolicy
from claw_v2.tools import ToolRegistry


class ToolRegistryTests(unittest.TestCase):
    def test_allowed_tools_respect_agent_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            registry = ToolRegistry.default(workspace_root=workspace)
            researcher_tools = registry.allowed_tools("researcher")
            self.assertIn("WebSearch", researcher_tools)
            self.assertNotIn("Write", researcher_tools)

    def test_write_tool_respects_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            registry = ToolRegistry.default(workspace_root=workspace)
            policy = SandboxPolicy(workspace_root=workspace)
            target = workspace / "notes.txt"
            result = registry.execute(
                "Write",
                {"path": str(target), "content": "hello"},
                agent_class="operator",
                policy=policy,
            )
            self.assertEqual(result["written"], 5)
            self.assertEqual(target.read_text(encoding="utf-8"), "hello")

    def test_search_memory_returns_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            memory = MemoryStore(root / "memory.db")
            memory.store_fact("profile.name", "Hector", source="profile", source_trust="trusted")
            registry = ToolRegistry.default(workspace_root=workspace, memory=memory)
            result = registry.execute("SearchMemory", {"query": "Hector"}, agent_class="researcher")
            self.assertEqual(len(result["matches"]), 1)

    def test_operator_cannot_use_web_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            registry = ToolRegistry.default(workspace_root=workspace)
            with self.assertRaises(PermissionError):
                registry.execute(
                    "WebSearch",
                    {"url": "https://example.com", "allowed_domains": ["example.com"]},
                    agent_class="operator",
                    policy=SandboxPolicy(workspace_root=workspace),
                    network_enforcer=DomainAllowlistEnforcer(),
                )


if __name__ == "__main__":
    unittest.main()
