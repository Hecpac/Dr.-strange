from __future__ import annotations

import tempfile
import unittest
from os import environ
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from claw_v2.adapters.anthropic import (
    ClaudeSDKExecutor,
    IDENTITY_OVERRIDE,
    SILENCE_DIRECTIVE,
    create_claude_sdk_executor,
    _detached_process_reason,
    _inline_browser_drive_reason,
    _safe_runtime_policy_reason,
    _tool_input_evidence,
    _tool_response_evidence,
)
from claw_v2.adapters.anthropic_hooks import make_pre_tool_use_hook
from claw_v2.adapters.base import AdapterError, AdapterUnavailableError, LLMRequest
from claw_v2.llm import LLMRouter
from claw_v2.observe import ObserveStream

from tests.helpers import make_config


class AnthropicIntegrationTests(unittest.TestCase):
    def test_tool_input_evidence_keeps_paths_and_commands_only(self) -> None:
        self.assertEqual(
            _tool_input_evidence(
                "Write",
                {"file_path": "notes/a.txt", "content": "secret body"},
            ),
            {"file_path": "notes/a.txt"},
        )
        self.assertEqual(
            _tool_input_evidence("Bash", {"command": "pytest -q"}),
            {"command": "pytest -q"},
        )

    def test_tool_input_evidence_omits_unknown_tool_inputs(self) -> None:
        self.assertEqual(
            _tool_input_evidence("UnknownTool", {"token": "abc", "file_path": "x"}),
            {},
        )

    def test_tool_input_evidence_redacts_secret_shaped_commands(self) -> None:
        evidence = _tool_input_evidence(
            "Bash",
            {"command": "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456' https://example.test"},
        )

        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", evidence["command"])
        self.assertIn("[REDACTED]", evidence["command"])

    def test_tool_response_evidence_keeps_safe_bash_markers_without_raw_stdout(self) -> None:
        evidence = _tool_response_evidence(
            "Bash",
            {
                "returncode": 0,
                "stdout": '{"ok": true, "message_id": 12715, "bytes": 123, "token": "secret"}\n',
            },
        )

        self.assertEqual(evidence["returncode"], 0)
        self.assertEqual(evidence["stdout_chars"], 67)
        self.assertIn("stdout_sha256", evidence)
        self.assertEqual(evidence["json_markers"], [{"ok": True, "bytes": 123, "message_id": 12715}])
        serialized = str(evidence)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("stdout", serialized.replace("stdout_chars", "").replace("stdout_sha256", ""))

    def test_runtime_policy_reason_hides_raw_whitelist_error(self) -> None:
        reason = _safe_runtime_policy_reason(
            "binary 'brew' requires higher privilege level (not in the allowed whitelist)"
        )

        self.assertEqual(reason, "command 'brew' is blocked by local execution policy")
        self.assertNotIn("allowed whitelist", reason)
        self.assertNotIn("higher privilege level", reason)

    def test_executor_fails_explicitly_when_sdk_package_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            executor = create_claude_sdk_executor(config)
            router = LLMRouter.default(config, anthropic_executor=executor)
            with patch("claw_v2.adapters.anthropic.import_module", side_effect=ModuleNotFoundError):
                with self.assertRaises(AdapterUnavailableError):
                    router.ask("hello", lane="brain", system_prompt="You are Claw.")


class AnthropicExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_multimodal_query_uses_resumed_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            executor = ClaudeSDKExecutor(config)
            recorded: dict[str, object] = {}

            class FakeAssistantMessage:
                def __init__(self, content, model) -> None:
                    self.content = content
                    self.model = model

            class FakeResultMessage:
                def __init__(self, *, session_id: str, total_cost_usd: float, usage: dict, result: str, is_error: bool) -> None:
                    self.session_id = session_id
                    self.total_cost_usd = total_cost_usd
                    self.usage = usage
                    self.result = result
                    self.is_error = is_error

            class FakeHookMatcher:
                def __init__(self, *, hooks) -> None:
                    self.hooks = hooks

            class FakeClaudeAgentOptions:
                def __init__(self, **kwargs) -> None:
                    self.kwargs = kwargs

            class FakeClaudeSDKClient:
                def __init__(self, options=None) -> None:
                    recorded["options"] = options

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                async def query(self, prompt, session_id: str = "default") -> None:
                    recorded["session_id"] = session_id
                    streamed: list[dict] = []
                    if isinstance(prompt, str):
                        recorded["prompt_type"] = "text"
                    else:
                        recorded["prompt_type"] = "stream"
                        async for item in prompt:
                            streamed.append(item)
                    recorded["streamed"] = streamed

                async def receive_response(self):
                    yield FakeAssistantMessage([SimpleNamespace(text="ok")], "claude-opus-4-7")
                    yield FakeResultMessage(
                        session_id="sdk-session-1",
                        total_cost_usd=0.1,
                        usage={},
                        result="ok",
                        is_error=False,
                    )

            fake_sdk = SimpleNamespace(
                ClaudeSDKClient=FakeClaudeSDKClient,
                ClaudeAgentOptions=FakeClaudeAgentOptions,
                HookMatcher=FakeHookMatcher,
                AssistantMessage=FakeAssistantMessage,
                ResultMessage=FakeResultMessage,
            )
            fake_sdk_types = SimpleNamespace(
                PermissionResultAllow=lambda **kwargs: SimpleNamespace(**kwargs),
                PermissionResultDeny=lambda **kwargs: SimpleNamespace(**kwargs),
            )
            request = LLMRequest(
                prompt=[
                    {"type": "text", "text": "que ves?"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "cG5n",
                        },
                    },
                ],
                system_prompt="You are Claw.",
                lane="brain",
                provider="anthropic",
                model="claude-opus-4-7",
                effort="high",
                session_id="resume-123",
                max_budget=0.5,
                evidence_pack={"app_session_id": "tg-1"},
                allowed_tools=None,
                agents=None,
                hooks=None,
                timeout=30.0,
                cwd=str(config.workspace_root),
            )

            with patch.dict(environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
                with patch("claw_v2.adapters.anthropic._load_sdk", return_value=fake_sdk):
                    with patch("claw_v2.adapters.anthropic._load_sdk_types", return_value=fake_sdk_types):
                        response = await executor._run(request)

            self.assertEqual(response.content, "ok")
            self.assertEqual(recorded["options"].kwargs["setting_sources"], [])
            self.assertEqual(recorded["options"].kwargs["extra_args"], {"disable-slash-commands": None})
            self.assertEqual(recorded["options"].kwargs["permission_mode"], "bypassPermissions")
            self.assertTrue(callable(recorded["options"].kwargs["stderr"]))
            self.assertEqual(recorded["options"].kwargs["env"], {"ANTHROPIC_API_KEY": ""})
            self.assertEqual(recorded["prompt_type"], "stream")
            self.assertEqual(recorded["session_id"], "resume-123")
            streamed = recorded["streamed"]
            self.assertEqual(len(streamed), 1)
            self.assertEqual(streamed[0]["message"]["content"][1]["type"], "image")

    async def test_api_key_mode_passes_key_and_uses_bare_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            config.claude_auth_mode = "api_key"
            executor = ClaudeSDKExecutor(config)

            class FakeHookMatcher:
                def __init__(self, *, hooks) -> None:
                    self.hooks = hooks

            class FakeClaudeAgentOptions:
                def __init__(self, **kwargs) -> None:
                    self.kwargs = kwargs

            fake_sdk = SimpleNamespace(
                ClaudeAgentOptions=FakeClaudeAgentOptions,
                HookMatcher=FakeHookMatcher,
                AgentDefinition=lambda **kwargs: kwargs,
            )

            request = LLMRequest(
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
                timeout=30.0,
                cwd=str(config.workspace_root),
            )

            with patch.dict(environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
                options = executor._build_options(fake_sdk, request)

            self.assertEqual(options.kwargs["extra_args"], {"disable-slash-commands": None, "bare": None})
            self.assertEqual(options.kwargs["env"]["ANTHROPIC_API_KEY"], "sk-test")

    async def test_brain_lane_appends_silence_directive_to_claude_code_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            executor = ClaudeSDKExecutor(config)

            class FakeHookMatcher:
                def __init__(self, *, hooks) -> None:
                    self.hooks = hooks

            class FakeClaudeAgentOptions:
                def __init__(self, **kwargs) -> None:
                    self.kwargs = kwargs

            fake_sdk = SimpleNamespace(
                ClaudeAgentOptions=FakeClaudeAgentOptions,
                HookMatcher=FakeHookMatcher,
                AgentDefinition=lambda **kwargs: kwargs,
            )
            request = LLMRequest(
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
                timeout=30.0,
                cwd=str(config.workspace_root),
            )

            options = executor._build_options(fake_sdk, request)

            system_prompt = options.kwargs["system_prompt"]
            self.assertEqual(system_prompt["type"], "preset")
            self.assertEqual(system_prompt["preset"], "claude_code")
            self.assertIn("You are Claw.", system_prompt["append"])
            self.assertIn("headless engine", system_prompt["append"])
            self.assertIn(SILENCE_DIRECTIVE.strip(), system_prompt["append"])
            # Identity-override block must be prepended so Dr. Strange persona
            # wins over the Claude Code preset's default "I am Claude" identity.
            self.assertIn("Dr. Strange", system_prompt["append"])
            self.assertIn(IDENTITY_OVERRIDE.strip().splitlines()[0], system_prompt["append"])
            self.assertLess(
                system_prompt["append"].index("Dr. Strange"),
                system_prompt["append"].index("You are Claw."),
                "IDENTITY_OVERRIDE must come before the persona system prompt",
            )
            self.assertEqual(options.kwargs["setting_sources"], [])
            self.assertEqual(options.kwargs["extra_args"], {"disable-slash-commands": None})

    async def test_executor_emits_llm_error_event_when_sdk_result_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = make_config(root)
            observe = ObserveStream(config.db_path)
            executor = ClaudeSDKExecutor(config, observe=observe)

            class FakeHookMatcher:
                def __init__(self, *, hooks) -> None:
                    self.hooks = hooks

            class FakeAssistantMessage:
                def __init__(self, content, model) -> None:
                    self.content = content
                    self.model = model

            class FakeResultMessage:
                def __init__(self, *, session_id: str, result: str, is_error: bool) -> None:
                    self.session_id = session_id
                    self.total_cost_usd = 0.0
                    self.usage = {}
                    self.result = result
                    self.is_error = is_error

            class FakeClaudeAgentOptions:
                def __init__(self, **kwargs) -> None:
                    self.kwargs = kwargs

            class FakeClaudeSDKClient:
                def __init__(self, options=None) -> None:
                    self.options = options

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                async def query(self, prompt, session_id: str = "default") -> None:
                    return None

                async def receive_response(self):
                    yield FakeAssistantMessage([SimpleNamespace(text="partial output")], "claude-opus-4-7")
                    yield FakeResultMessage(
                        session_id="sdk-session-error",
                        result="tool runtime exploded",
                        is_error=True,
                    )

            fake_sdk = SimpleNamespace(
                ClaudeSDKClient=FakeClaudeSDKClient,
                ClaudeAgentOptions=FakeClaudeAgentOptions,
                HookMatcher=FakeHookMatcher,
                AssistantMessage=FakeAssistantMessage,
                ResultMessage=FakeResultMessage,
            )
            fake_sdk_types = SimpleNamespace(
                PermissionResultAllow=lambda **kwargs: SimpleNamespace(**kwargs),
                PermissionResultDeny=lambda **kwargs: SimpleNamespace(**kwargs),
            )
            request = LLMRequest(
                prompt="hello",
                system_prompt="You are Claw.",
                lane="brain",
                provider="anthropic",
                model="claude-opus-4-7",
                effort="high",
                session_id="resume-err",
                max_budget=0.5,
                evidence_pack={"app_session_id": "tg-1"},
                allowed_tools=None,
                agents=None,
                hooks=None,
                timeout=30.0,
                cwd=str(config.workspace_root),
            )

            with patch.dict(environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
                with patch("claw_v2.adapters.anthropic._load_sdk", return_value=fake_sdk):
                    with patch("claw_v2.adapters.anthropic._load_sdk_types", return_value=fake_sdk_types):
                        with self.assertRaises(AdapterError):
                            await executor._run(request)

            event = observe.recent_events(limit=1)[0]
            self.assertEqual(event["event_type"], "llm_error")
            self.assertEqual(event["provider"], "anthropic")
            self.assertEqual(event["payload"]["session_id"], "resume-err")
            self.assertEqual(event["payload"]["query_session_id"], "resume-err")
            self.assertEqual(event["payload"]["result_session_id"], "sdk-session-error")
            self.assertEqual(event["payload"]["error_type"], "AdapterError")
            self.assertIn("tool runtime exploded", event["payload"]["error"])
            self.assertIn("partial output", event["payload"]["partial_text_preview"])


class ExtendedThinkingWiringTests(unittest.IsolatedAsyncioTestCase):
    """Per-lane thinking budget must flow into ClaudeAgentOptions."""

    def _make_request(self, *, lane: str, thinking_tokens: int) -> LLMRequest:
        return LLMRequest(
            prompt="hello",
            system_prompt=None,
            lane=lane,
            provider="anthropic",
            model="claude-opus-4-7",
            effort="high",
            session_id=None,
            max_budget=0.5,
            evidence_pack={"app_session_id": "tg-1"},
            allowed_tools=None,
            agents=None,
            hooks=None,
            timeout=30.0,
            thinking_tokens=thinking_tokens,
        )

    def _fake_sdk(self):
        class FakeHookMatcher:
            def __init__(self, *, hooks) -> None:
                self.hooks = hooks

        class FakeClaudeAgentOptions:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs

        return SimpleNamespace(
            ClaudeAgentOptions=FakeClaudeAgentOptions,
            HookMatcher=FakeHookMatcher,
            AgentDefinition=lambda **kwargs: kwargs,
        )

    async def test_thinking_tokens_zero_omits_thinking_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            executor = ClaudeSDKExecutor(config)
            request = self._make_request(lane="brain", thinking_tokens=0)
            options = executor._build_options(self._fake_sdk(), request)
            self.assertNotIn("thinking", options.kwargs)
            self.assertNotIn("max_thinking_tokens", options.kwargs)

    async def test_thinking_tokens_positive_sets_enabled_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            executor = ClaudeSDKExecutor(config)
            request = self._make_request(lane="verifier", thinking_tokens=4096)
            options = executor._build_options(self._fake_sdk(), request)
            self.assertEqual(
                options.kwargs["thinking"],
                {"type": "enabled", "budget_tokens": 4096},
            )
            self.assertEqual(options.kwargs["max_thinking_tokens"], 4096)


class DelegationMcpServerTests(unittest.IsolatedAsyncioTestCase):
    """The brain-lane delegate_task MCP server attach + tool body contract."""

    def _fake_sdk(self) -> SimpleNamespace:
        class FakeHookMatcher:
            def __init__(self, *, hooks) -> None:
                self.hooks = hooks

        class FakeClaudeAgentOptions:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs

        def fake_tool(name, description, schema):
            def decorator(fn):
                return SimpleNamespace(
                    name=name, description=description, schema=schema, handler=fn
                )

            return decorator

        return SimpleNamespace(
            ClaudeAgentOptions=FakeClaudeAgentOptions,
            HookMatcher=FakeHookMatcher,
            AgentDefinition=lambda **kwargs: kwargs,
            tool=fake_tool,
            create_sdk_mcp_server=lambda **kwargs: SimpleNamespace(**kwargs),
        )

    def _make_request(self, *, lane: str, delegation_handler) -> LLMRequest:
        return LLMRequest(
            prompt="hello",
            system_prompt="You are Claw.",
            lane=lane,
            provider="anthropic",
            model="claude-opus-4-7",
            effort="high",
            session_id=None,
            max_budget=0.5,
            evidence_pack={"app_session_id": "tg-1"},
            allowed_tools=None,
            agents=None,
            hooks=None,
            timeout=30.0,
            delegation_handler=delegation_handler,
        )

    async def test_brain_lane_with_delegation_handler_attaches_claw_mcp_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ClaudeSDKExecutor(make_config(Path(tmpdir)))
            request = self._make_request(
                lane="brain", delegation_handler=lambda payload: {"ack": "ok"}
            )

            options = executor._build_options(self._fake_sdk(), request)

            server = options.kwargs["mcp_servers"]["claw"]
            self.assertEqual(server.name, "claw")
            self.assertEqual(len(server.tools), 1)
            self.assertEqual(server.tools[0].name, "delegate_task")
            self.assertEqual(server.tools[0].schema["required"], ["objective"])

    async def test_mcp_server_not_attached_for_worker_lane_or_missing_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ClaudeSDKExecutor(make_config(Path(tmpdir)))

            worker_request = self._make_request(
                lane="worker", delegation_handler=lambda payload: {"ack": "ok"}
            )
            worker_options = executor._build_options(self._fake_sdk(), worker_request)
            self.assertNotIn("mcp_servers", worker_options.kwargs)

            no_handler_request = self._make_request(lane="brain", delegation_handler=None)
            no_handler_options = executor._build_options(self._fake_sdk(), no_handler_request)
            self.assertNotIn("mcp_servers", no_handler_options.kwargs)

    async def test_delegate_task_tool_invokes_handler_and_returns_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ClaudeSDKExecutor(make_config(Path(tmpdir)))
            calls: list[dict] = []

            def handler(payload: dict) -> dict:
                calls.append(payload)
                return {"ok": True, "ack": "Tarea autónoma iniciada: `t-1`"}

            request = self._make_request(lane="brain", delegation_handler=handler)
            options = executor._build_options(self._fake_sdk(), request)
            delegate = options.kwargs["mcp_servers"]["claw"].tools[0]

            result = await delegate.handler(
                {"objective": "Publica el grid", "mode": "publish", "reason": "long job"}
            )
            self.assertNotIn("is_error", result)
            self.assertIn("Tarea autónoma iniciada", result["content"][0]["text"])
            self.assertEqual(
                calls,
                [{"objective": "Publica el grid", "mode": "publish", "reason": "long job"}],
            )

            blank = await delegate.handler({"objective": "   "})
            self.assertTrue(blank["is_error"])
            self.assertEqual(len(calls), 1, "handler must not run for blank objectives")

            bad_mode = await delegate.handler({"objective": "x", "mode": "warp"})
            self.assertTrue(bad_mode["is_error"])
            self.assertEqual(len(calls), 1)

    async def test_delegate_task_tool_bounds_handler_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = ClaudeSDKExecutor(make_config(Path(tmpdir)))

            def handler(payload: dict) -> dict:
                raise RuntimeError("ledger exploded " + "x" * 500)

            request = self._make_request(lane="brain", delegation_handler=handler)
            options = executor._build_options(self._fake_sdk(), request)
            delegate = options.kwargs["mcp_servers"]["claw"].tools[0]

            result = await delegate.handler({"objective": "do it"})
            self.assertTrue(result["is_error"])
            self.assertLess(len(result["content"][0]["text"]), 400)


class InlineBrowserDriveGuardTests(unittest.TestCase):
    """Backstop that keeps the brain from driving Chrome/CDP/desktop inline."""

    def test_detects_direct_command_markers(self) -> None:
        for command in (
            "/opt/homebrew/bin/peekaboo image --app ChatGPT",
            "curl -s http://localhost:9250/json/list",
            "python3 -c \"import requests; requests.get('http://127.0.0.1:9222/json/version')\"",
            "npx playwright open https://instagram.com",
        ):
            self.assertIsNotNone(
                _inline_browser_drive_reason("Bash", {"command": command}), command
            )

    def test_detects_markers_inside_referenced_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            script = Path(tmpdir) / "_ig_publish.py"
            script.write_text(
                "import socket\nHOST, PORT = 'localhost', 9250\nWS = 'webSocketDebuggerUrl'\n",
                encoding="utf-8",
            )
            reason = _inline_browser_drive_reason(
                "Bash", {"command": f"/usr/bin/python3 {script}"}
            )
            self.assertIsNotNone(reason)

    def test_ignores_benign_and_non_bash(self) -> None:
        self.assertIsNone(_inline_browser_drive_reason("Bash", {"command": "git status"}))
        self.assertIsNone(
            _inline_browser_drive_reason("Bash", {"command": "grep -r TODO claw_v2/"})
        )
        self.assertIsNone(_inline_browser_drive_reason("Bash", {"command": ""}))
        self.assertIsNone(
            _inline_browser_drive_reason("Read", {"file_path": "/x/peekaboo.py"})
        )

    def test_missing_script_path_does_not_raise(self) -> None:
        self.assertIsNone(
            _inline_browser_drive_reason(
                "Bash", {"command": "/usr/bin/python3 /nonexistent/run.py --flag"}
            )
        )


class DetachedProcessGuardTests(unittest.TestCase):
    """Backstop that keeps the brain from improvising ghost background
    processes (no ledger, no monitor, no notification) instead of delegating
    durable work (T12, 2026-06-12)."""

    def test_detects_detached_launchers(self) -> None:
        for command in (
            "nohup python3 fetch_model.py > /tmp/fetch.log 2>&1 &",
            "setsid brew install ffmpeg",
            "python3 long_job.py & disown",
        ):
            self.assertIsNotNone(
                _detached_process_reason("Bash", {"command": command}), command
            )

    def test_detects_long_running_background_tail(self) -> None:
        for command in (
            "brew install ffmpeg && make -j8 &",
            "curl -L -o model.bin https://example.com/model.bin &",
            "wget https://example.com/dataset.tar.gz > /tmp/dl.log 2>&1 &",
            "pip install torch > /tmp/pip.log &",
            "sleep 600 && ./check.sh &",
        ):
            self.assertIsNotNone(
                _detached_process_reason("Bash", {"command": command}), command
            )

    def test_allows_trivial_background_and_foreground_commands(self) -> None:
        for command in (
            "ls -la &",
            "git status",
            "curl -s https://api.example.com/health",
            "pip install requests",
            "echo done && touch /tmp/marker &",
            "",
        ):
            self.assertIsNone(
                _detached_process_reason("Bash", {"command": command}), command
            )

    def test_ignores_non_bash(self) -> None:
        self.assertIsNone(
            _detached_process_reason("Read", {"file_path": "/x/nohup.out"})
        )


class DetachedProcessHookLaneTests(unittest.IsolatedAsyncioTestCase):
    """Brain lane denies detached launches; worker lanes are not gated."""

    @staticmethod
    def _hook(lane: str):
        request = SimpleNamespace(lane=lane, model="m", evidence_pack=None)
        runtime_policy = SimpleNamespace(enforce=lambda *a, **k: None)
        return make_pre_tool_use_hook(
            request, runtime_policy=runtime_policy, observe=None
        )

    async def test_brain_lane_denies_detached_launch(self) -> None:
        hook = self._hook("brain")
        result = await hook(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "nohup python3 fetch.py > /tmp/f.log 2>&1 &"},
            },
            None,
            None,
        )
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertIn("delegate_task", result["hookSpecificOutput"]["permissionDecisionReason"])

    async def test_worker_lane_allows_detached_launch(self) -> None:
        hook = self._hook("worker")
        result = await hook(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "nohup python3 fetch.py > /tmp/f.log 2>&1 &"},
            },
            None,
            None,
        )
        self.assertEqual(result, {"continue_": True})


if __name__ == "__main__":
    unittest.main()
