from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.runtime_policy import RuntimePolicyEngine, sanitize_child_env, _iter_path_values
from claw_v2.sandbox import SandboxPolicy
from claw_v2.tools import ToolDefinition, ToolRegistry


class RuntimePolicyEngineTests(unittest.TestCase):
    def test_sanitize_child_env_preserves_allowlist_and_drops_secrets(self) -> None:
        result = sanitize_child_env(
            {
                "PATH": "/bin",
                "HOME": "/home/test",
                "TERM": "xterm",
                "OPENAI_API_KEY": "sk-secret",
                "SESSION_TOKEN": "tok",
                "CUSTOM_FLAG": "1",
                "AUTH_MODE": "api",
            }
        )

        self.assertEqual(result.env["PATH"], "/bin")
        self.assertEqual(result.env["HOME"], "/home/test")
        self.assertEqual(result.env["TERM"], "xterm")
        self.assertNotIn("OPENAI_API_KEY", result.env)
        self.assertNotIn("SESSION_TOKEN", result.env)
        self.assertNotIn("CUSTOM_FLAG", result.env)
        self.assertNotIn("AUTH_MODE", result.env)
        self.assertEqual(result.dropped_sensitive_count, 3)
        self.assertEqual(result.to_metadata()["preserved_count"], 3)

    def test_autoexec_max_tier_is_a_ceiling_never_an_override(self) -> None:
        # AM-T3FLOOR (2026-06-12): Tier 3 always hits the approval gate. A
        # misconfigured autoexec_max_tier=3 must be clamped, and the tier>=3
        # floor in enforce() is unconditional.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            gate_calls: list[str] = []

            def gate(definition, args) -> None:
                gate_calls.append(definition.name)

            engine = RuntimePolicyEngine(
                workspace_root=workspace,
                sandbox_policy=SandboxPolicy(workspace_root=workspace),
                approval_gate=gate,
                autoexec_max_tier=3,
            )
            self.assertEqual(engine.autoexec_max_tier, 2)

            decision = engine.enforce(
                "HeyGenVideo",
                {},
                context="operator",
                tier=3,
            )
            self.assertTrue(decision.approval_required)
            self.assertEqual(gate_calls, ["HeyGenVideo"])

    def test_unknown_tool_is_denied_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            engine = RuntimePolicyEngine(
                workspace_root=workspace, sandbox_policy=SandboxPolicy(workspace_root=workspace)
            )

            with self.assertRaises(PermissionError) as ctx:
                engine.enforce("not.in.policy", {}, context="operator")

            self.assertIn("not declared", str(ctx.exception))

    def test_delegate_task_policy_allows_brain_and_denies_worker_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            engine = RuntimePolicyEngine(
                workspace_root=workspace, sandbox_policy=SandboxPolicy(workspace_root=workspace)
            )

            decision = engine.enforce(
                "mcp__claw__delegate_task",
                {"objective": "Publica el grid", "mode": "publish"},
                context="brain",
            )
            self.assertIsNotNone(decision)

            for denied_context in ("worker", "worker_heavy", "telegram", "daemon"):
                with self.assertRaises(PermissionError, msg=denied_context):
                    engine.enforce(
                        "mcp__claw__delegate_task",
                        {"objective": "x"},
                        context=denied_context,
                    )

            with self.assertRaises(PermissionError) as ctx:
                engine.enforce("mcp__claw__other", {}, context="brain")
            self.assertIn("not declared", str(ctx.exception))

    def test_secret_paths_are_blocked_for_any_disk_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / ".env").write_text("SECRET=1", encoding="utf-8")
            engine = RuntimePolicyEngine(
                workspace_root=workspace, sandbox_policy=SandboxPolicy(workspace_root=workspace)
            )

            with self.assertRaises(PermissionError):
                engine.enforce("Read", {"path": ".env"}, context="operator")

    def test_read_only_policy_rejects_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "README.md").write_text("ok", encoding="utf-8")
            engine = RuntimePolicyEngine(
                workspace_root=workspace, sandbox_policy=SandboxPolicy(workspace_root=workspace)
            )

            with self.assertRaises(PermissionError) as ctx:
                engine.enforce(
                    "Read", {"path": "README.md"}, context="operator", mutates_state=True
                )

            self.assertIn("read-only", str(ctx.exception))

    def test_explicit_non_http_url_is_denied_for_network_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            engine = RuntimePolicyEngine(
                workspace_root=workspace,
                sandbox_policy=SandboxPolicy(workspace_root=workspace),
            )

            with self.assertRaises(PermissionError) as ctx:
                engine.enforce(
                    "BrowserNavigate",
                    {"url": "file:///etc/passwd"},
                    context="operator",
                    requires_network=True,
                )

            self.assertIn("network target blocked", str(ctx.exception))

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

    def test_bash_dash_c_payload_cannot_escape_path_boundary(self) -> None:
        # 2026-05-29 audit CRITICAL: bash -c '<cmd>' left the -c payload as one
        # opaque shlex token that resolved inside the workspace, so absolute
        # host paths slipped past the boundary check.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            engine = RuntimePolicyEngine(
                workspace_root=workspace, sandbox_policy=SandboxPolicy(workspace_root=workspace)
            )
            for command in (
                "bash -c 'cat /etc/passwd'",
                "bash -c 'cp /etc/passwd /tmp/x'",
                "bash -c 'grep -r AKIA /var/log'",
                "sh -c 'cat /etc/hosts'",
            ):
                with self.subTest(command=command):
                    with self.assertRaises(PermissionError):
                        engine.enforce("Bash", {"command": command}, context="operator")

    def test_bash_dash_c_workspace_path_still_allowed(self) -> None:
        # Regression guard: the unwrap fix must not over-block legitimate
        # workspace-relative reads inside bash -c.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "README.md").write_text("ok", encoding="utf-8")
            engine = RuntimePolicyEngine(
                workspace_root=workspace, sandbox_policy=SandboxPolicy(workspace_root=workspace)
            )
            engine.enforce("Bash", {"command": "bash -c 'cat README.md'"}, context="operator")

    def test_direct_secret_command_path_blocked(self) -> None:
        # Regression guard: direct (non-wrapped) host path stays blocked.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            engine = RuntimePolicyEngine(
                workspace_root=workspace, sandbox_policy=SandboxPolicy(workspace_root=workspace)
            )
            with self.assertRaises(PermissionError):
                engine.enforce("Bash", {"command": "cat /etc/passwd"}, context="operator")

    def test_iter_path_values_recurses_into_lists(self) -> None:
        # 2026-05-29 audit: _iter_path_values skipped lists (asymmetric with
        # _iter_urls), so path args inside lists escaped the secret/boundary check.
        found = [
            v
            for _, v in _iter_path_values(
                {"files": [{"path": "/etc/passwd"}, {"path": "~/.netrc"}]}
            )
        ]
        self.assertIn("/etc/passwd", found)
        self.assertIn("~/.netrc", found)
        nested = [v for _, v in _iter_path_values({"path": ["/etc/passwd", "~/.npmrc"]})]
        self.assertIn("/etc/passwd", nested)
        # PR1 follow-up: non-scalars nested under a path key must not bypass extraction.
        nested_dict = [v for _, v in _iter_path_values({"path": [{"path": "/etc/passwd"}]})]
        self.assertIn("/etc/passwd", nested_dict)
        nested_list = [v for _, v in _iter_path_values({"path": [["/etc/passwd"]]})]
        self.assertIn("/etc/passwd", nested_list)


if __name__ == "__main__":
    unittest.main()
