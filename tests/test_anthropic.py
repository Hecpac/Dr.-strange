from __future__ import annotations

import asyncio
import tempfile
import unittest
from os import environ
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from claw_v2.adapters.anthropic import (
    ClaudeSDKExecutor,
    SILENCE_DIRECTIVE,
    create_claude_sdk_executor,
)
from claw_v2.adapters.base import AdapterError, AdapterUnavailableError, LLMRequest
from claw_v2.llm import LLMRouter
from claw_v2.observe import ObserveStream

from tests.helpers import make_config


class AnthropicIntegrationTests(unittest.TestCase):
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
            self.assertEqual(recorded["options"].kwargs["setting_sources"], ["project", "local"])
            self.assertEqual(recorded["options"].kwargs["extra_args"], {})
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

            self.assertEqual(options.kwargs["extra_args"], {"bare": None})
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

    async def test_executor_times_out_stalled_sdk_response_stream(self) -> None:
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
                pass

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
                    yield FakeAssistantMessage([SimpleNamespace(text="still working")], "claude-opus-4-7")
                    await asyncio.sleep(1)

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
                session_id="resume-timeout",
                max_budget=0.5,
                evidence_pack={"app_session_id": "tg-1"},
                allowed_tools=None,
                agents=None,
                hooks=None,
                timeout=0.01,
                cwd=str(config.workspace_root),
            )

            with patch.dict(environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
                with patch("claw_v2.adapters.anthropic._load_sdk", return_value=fake_sdk):
                    with patch("claw_v2.adapters.anthropic._load_sdk_types", return_value=fake_sdk_types):
                        with self.assertRaises(AdapterError) as ctx:
                            await executor._run(request)

            self.assertIn("timed out", str(ctx.exception))
            event = observe.recent_events(limit=1)[0]
            self.assertEqual(event["event_type"], "llm_error")
            self.assertEqual(event["payload"]["session_id"], "resume-timeout")
            self.assertEqual(event["payload"]["error_type"], "TimeoutError")
            self.assertIn("still working", event["payload"]["partial_text_preview"])


if __name__ == "__main__":
    unittest.main()
