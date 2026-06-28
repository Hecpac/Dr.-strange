"""D2 — safety net for the Claude SDK executor hooks (claw_v2/adapters/anthropic_hooks.py).

Covers the contracts that AH1/AH3 hardened:
- the inline browser/CDP backstop denies every pattern in the brain lane and
  stays out of the way for worker lanes;
- ApprovalPending / PermissionError from runtime policy become deny decisions
  with a systemMessage (never exceptions into the SDK);
- PostToolUseFailure still records the mutation (a failed tool may have
  partial side effects);
- record_tools_executed marks every exception path out of ClaudeSDKExecutor._run.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from claw_v2.adapters.anthropic import ClaudeSDKExecutor
from claw_v2.adapters.anthropic_hooks import (
    build_can_use_tool,
    build_hooks,
    make_post_tool_use_failure_hook,
    make_post_tool_use_hook,
    make_pre_tool_use_hook,
)
from claw_v2.adapters.base import (
    AdapterError,
    LLMRequest,
    tools_executed_before_failure,
)
from claw_v2.approval_gate import ApprovalPending
from claw_v2.runtime_policy import RuntimePolicyEngine
from claw_v2.sandbox import SandboxPolicy

from tests.helpers import make_config

# One representative command per backstop pattern in _INLINE_BROWSER_DRIVE_PATTERNS.
BACKSTOP_COMMANDS = (
    "/opt/homebrew/bin/peekaboo image --app ChatGPT",
    "npx playwright open https://instagram.com",
    "python3 -m selenium.webdriver",
    "chromedriver --port=4444",
    "cliclick c:100,200",
    "curl -s http://localhost:9250/json | grep webSocketDebuggerUrl",
    "curl -s http://localhost:9250/json/list",
    "curl -s http://127.0.0.1:9222/json/version",
    "lsof -i :9250",
    "lsof -i :9222",
    "python3 -m computer_use --demo",
)


class _AllowAllPolicy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str]] = []

    def enforce(self, tool_name: str, tool_input: dict, *, context: str) -> None:
        self.calls.append((tool_name, dict(tool_input or {}), context))


class _RaisingPolicy:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def enforce(self, tool_name: str, tool_input: dict, *, context: str) -> None:
        raise self.exc


def _request(lane: str) -> SimpleNamespace:
    return SimpleNamespace(
        lane=lane,
        model="claude-opus-4-7",
        evidence_pack={"app_session_id": "tg-1"},
        allowed_tools=None,
        hooks=None,
    )


def _approval_pending() -> ApprovalPending:
    return ApprovalPending(
        approval_id="ap-1", token="tok", tool="Bash", summary="git push needs approval"
    )


def _init_git_repo(path: Path, *, branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"], cwd=path, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "checkout", "-B", branch],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


class PreToolUseBackstopTests(unittest.IsolatedAsyncioTestCase):
    async def test_brain_lane_denies_every_backstop_pattern(self) -> None:
        policy = _AllowAllPolicy()
        hook = make_pre_tool_use_hook(_request("brain"), runtime_policy=policy, observe=None)
        for command in BACKSTOP_COMMANDS:
            result = await hook(
                {"tool_name": "Bash", "tool_input": {"command": command}}, "tu-1", None
            )
            decision = result.get("hookSpecificOutput", {})
            self.assertEqual(decision.get("permissionDecision"), "deny", command)
            self.assertIn("delegate_task", decision.get("permissionDecisionReason", ""), command)
            self.assertIn("Tool invocation blocked", result.get("systemMessage", ""), command)
        self.assertEqual(policy.calls, [], "denied calls must never reach runtime policy")

    async def test_worker_lanes_allow_backstop_patterns(self) -> None:
        for lane in ("worker", "worker_heavy"):
            policy = _AllowAllPolicy()
            hook = make_pre_tool_use_hook(_request(lane), runtime_policy=policy, observe=None)
            for command in BACKSTOP_COMMANDS:
                result = await hook(
                    {"tool_name": "Bash", "tool_input": {"command": command}},
                    "tu-1",
                    None,
                )
                self.assertEqual(result, {"continue_": True}, f"{lane}: {command}")
            self.assertEqual(len(policy.calls), len(BACKSTOP_COMMANDS))

    async def test_brain_lane_allows_benign_bash(self) -> None:
        policy = _AllowAllPolicy()
        hook = make_pre_tool_use_hook(_request("brain"), runtime_policy=policy, observe=None)
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "git status"}}, "tu-1", None
        )
        self.assertEqual(result, {"continue_": True})
        self.assertEqual(policy.calls, [("Bash", {"command": "git status"}, "brain")])


class PreToolUsePolicyDenyTests(unittest.IsolatedAsyncioTestCase):
    async def test_approval_pending_becomes_deny_with_system_message(self) -> None:
        pending = _approval_pending()
        hook = make_pre_tool_use_hook(
            _request("brain"), runtime_policy=_RaisingPolicy(pending), observe=None
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "git push"}}, "tu-1", None
        )
        decision = result["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertEqual(decision["permissionDecisionReason"], str(pending))
        self.assertEqual(result["systemMessage"], f"Tool invocation blocked: {pending}")

    async def test_permission_error_becomes_sanitized_deny(self) -> None:
        exc = PermissionError(
            "binary 'brew' requires higher privilege level (not in the allowed whitelist)"
        )
        hook = make_pre_tool_use_hook(
            _request("worker"), runtime_policy=_RaisingPolicy(exc), observe=None
        )
        result = await hook(
            {"tool_name": "Bash", "tool_input": {"command": "brew install x"}},
            "tu-1",
            None,
        )
        decision = result["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertNotIn("whitelist", decision["permissionDecisionReason"])
        self.assertIn("blocked by local execution policy", decision["permissionDecisionReason"])
        self.assertIn("Tool invocation blocked", result["systemMessage"])


class CanUseToolDenyTests(unittest.IsolatedAsyncioTestCase):
    def _sdk_types(self) -> SimpleNamespace:
        return SimpleNamespace(
            PermissionResultAllow=lambda **kwargs: SimpleNamespace(kind="allow", **kwargs),
            PermissionResultDeny=lambda **kwargs: SimpleNamespace(kind="deny", **kwargs),
        )

    async def test_approval_pending_denies_with_interrupt(self) -> None:
        pending = _approval_pending()
        can_use = build_can_use_tool(
            self._sdk_types(), _request("brain"), runtime_policy=_RaisingPolicy(pending)
        )
        result = await can_use("Bash", {"command": "git push"}, None)
        self.assertEqual(result.kind, "deny")
        self.assertEqual(result.message, str(pending))
        self.assertTrue(result.interrupt)

    async def test_permission_error_denies_with_sanitized_reason(self) -> None:
        exc = PermissionError(
            "binary 'brew' requires higher privilege level (not in the allowed whitelist)"
        )
        can_use = build_can_use_tool(
            self._sdk_types(), _request("worker"), runtime_policy=_RaisingPolicy(exc)
        )
        result = await can_use("Bash", {"command": "brew install x"}, None)
        self.assertEqual(result.kind, "deny")
        self.assertNotIn("whitelist", result.message)
        self.assertTrue(result.interrupt)

    async def test_git_commit_on_protected_branch_denies_before_sdk_tool_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            _init_git_repo(repo, branch="main")
            runtime_policy = RuntimePolicyEngine(
                workspace_root=repo,
                sandbox_policy=SandboxPolicy(workspace_root=repo),
            )
            can_use = build_can_use_tool(
                self._sdk_types(), _request("operator"), runtime_policy=runtime_policy
            )

            result = await can_use("Bash", {"command": "git commit -m blocked"}, None)

        self.assertEqual(result.kind, "deny")
        self.assertIn("protected branch", result.message)
        self.assertTrue(result.interrupt)

    async def test_allowlisted_tools_only(self) -> None:
        request = _request("worker")
        request.allowed_tools = ["Read"]
        can_use = build_can_use_tool(self._sdk_types(), request, runtime_policy=_AllowAllPolicy())
        denied = await can_use("Bash", {"command": "ls"}, None)
        self.assertEqual(denied.kind, "deny")
        allowed = await can_use("Read", {"file_path": "/tmp/x"}, None)
        self.assertEqual(allowed.kind, "allow")


class MutationTrackingTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_tool_use_failure_records_mutation(self) -> None:
        tracked: list[str] = []
        hook = make_post_tool_use_failure_hook(
            _request("brain"), observe=None, track_mutation=tracked.append
        )
        await hook(
            {
                "tool_name": "Bash",
                "tool_response": {"is_error": True, "stderr": "timeout"},
            },
            "tu-1",
            None,
        )
        self.assertEqual(tracked, ["Bash"])

    async def test_post_tool_use_records_mutation(self) -> None:
        tracked: list[str] = []
        hook = make_post_tool_use_hook(
            _request("brain"), observe=None, track_mutation=tracked.append
        )
        await hook({"tool_name": "Write", "tool_response": {}}, "tu-1", None)
        self.assertEqual(tracked, ["Write"])

    async def test_build_hooks_failure_path_skips_read_only_tools(self) -> None:
        class FakeHookMatcher:
            def __init__(self, *, hooks) -> None:
                self.hooks = hooks

        sdk = SimpleNamespace(HookMatcher=FakeHookMatcher)
        mutating: list[str] = []
        hooks = build_hooks(
            sdk,
            _request("brain"),
            runtime_policy=_AllowAllPolicy(),
            observe=None,
            mutation_tracker=mutating,
        )
        failure_hook = hooks["PostToolUseFailure"][0].hooks[0]
        await failure_hook({"tool_name": "Read", "tool_response": {"is_error": True}}, "t", None)
        self.assertEqual(mutating, [], "read-only tools are never counted as mutations")
        await failure_hook({"tool_name": "Bash", "tool_response": {"is_error": True}}, "t", None)
        self.assertEqual(mutating, ["Bash"], "a failed Bash still counts as a mutation")

    async def test_advisory_lanes_get_no_tool_hooks(self) -> None:
        class FakeHookMatcher:
            def __init__(self, *, hooks) -> None:
                self.hooks = hooks

        sdk = SimpleNamespace(HookMatcher=FakeHookMatcher)
        hooks = build_hooks(
            sdk,
            _request("verifier"),
            runtime_policy=_AllowAllPolicy(),
            observe=None,
            mutation_tracker=[],
        )
        self.assertNotIn("PreToolUse", hooks)
        self.assertNotIn("PostToolUse", hooks)
        self.assertNotIn("PostToolUseFailure", hooks)


class RecordToolsExecutedOnFailureTests(unittest.IsolatedAsyncioTestCase):
    """Every exception path out of _run must carry tools_executed_before_failure."""

    def _request(self, *, timeout: float = 30.0) -> LLMRequest:
        return LLMRequest(
            prompt="hello",
            system_prompt="You are Claw.",
            lane="brain",
            provider="anthropic",
            model="claude-opus-4-7",
            effort="high",
            session_id=None,
            max_budget=0.5,
            evidence_pack={"app_session_id": "tg-1"},
            allowed_tools=None,
            agents=None,
            hooks=None,
            timeout=timeout,
        )

    def _executor_with_mutation(self, config, *, mutated_tool: str = "Bash") -> ClaudeSDKExecutor:
        executor = ClaudeSDKExecutor(config)

        def _build_options(sdk, request, *, stderr_callback=None, mutation_tracker=None):
            # Simulate a turn where a mutating tool already executed before the
            # failure: the PostToolUse hook would have appended to the tracker.
            if mutation_tracker is not None:
                mutation_tracker.append(mutated_tool)
            return SimpleNamespace(stderr=stderr_callback)

        executor._build_options = _build_options  # type: ignore[method-assign]
        return executor

    def _fake_sdk(self, receive_factory) -> SimpleNamespace:
        class FakeAssistantMessage:
            def __init__(self, content, model) -> None:
                self.content = content
                self.model = model

        class FakeResultMessage:
            def __init__(self, **kwargs) -> None:
                self.__dict__.update(kwargs)

        class FakeClaudeSDKClient:
            def __init__(self, options=None) -> None:
                self.options = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def query(self, prompt, session_id: str = "default") -> None:
                return None

            def receive_response(self):
                return receive_factory()

        return SimpleNamespace(
            ClaudeSDKClient=FakeClaudeSDKClient,
            AssistantMessage=FakeAssistantMessage,
            ResultMessage=FakeResultMessage,
        )

    async def test_sdk_is_error_result_records_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            executor = self._executor_with_mutation(config)
            sdk: SimpleNamespace | None = None

            async def receive():
                yield sdk.ResultMessage(
                    session_id="s-1",
                    total_cost_usd=0.0,
                    usage={},
                    result="tool runtime exploded",
                    is_error=True,
                )

            sdk = self._fake_sdk(receive)
            with patch("claw_v2.adapters.anthropic._load_sdk", return_value=sdk):
                with self.assertRaises(AdapterError) as ctx:
                    await executor._run(self._request())
            self.assertEqual(tools_executed_before_failure(ctx.exception), ["Bash"])

    async def test_generic_exception_records_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            executor = self._executor_with_mutation(config)

            async def receive():
                raise RuntimeError("transport died")
                yield  # pragma: no cover

            sdk = self._fake_sdk(receive)
            with patch("claw_v2.adapters.anthropic._load_sdk", return_value=sdk):
                with self.assertRaises(AdapterError) as ctx:
                    await executor._run(self._request())
            self.assertEqual(tools_executed_before_failure(ctx.exception), ["Bash"])

    async def test_generic_exception_with_stderr_records_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            executor = ClaudeSDKExecutor(config)

            def _build_options(sdk, request, *, stderr_callback=None, mutation_tracker=None):
                if mutation_tracker is not None:
                    mutation_tracker.append("Write")
                if stderr_callback is not None:
                    stderr_callback("cli: fatal stream error")
                return SimpleNamespace(stderr=stderr_callback)

            executor._build_options = _build_options  # type: ignore[method-assign]

            async def receive():
                raise RuntimeError("transport died")
                yield  # pragma: no cover

            sdk = self._fake_sdk(receive)
            with patch("claw_v2.adapters.anthropic._load_sdk", return_value=sdk):
                with self.assertRaises(AdapterError) as ctx:
                    await executor._run(self._request())
            self.assertIn("fatal stream error", str(ctx.exception))
            self.assertEqual(tools_executed_before_failure(ctx.exception), ["Write"])

    async def test_timeout_records_tools_and_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            executor = self._executor_with_mutation(config)

            async def receive():
                await asyncio.sleep(5)
                yield  # pragma: no cover

            sdk = self._fake_sdk(receive)
            with patch("claw_v2.adapters.anthropic._load_sdk", return_value=sdk):
                with self.assertRaises(AdapterError) as ctx:
                    await executor._run(self._request(timeout=0.05))
            self.assertEqual(ctx.exception.metadata.get("reason"), "timeout")
            self.assertEqual(tools_executed_before_failure(ctx.exception), ["Bash"])


if __name__ == "__main__":
    unittest.main()
