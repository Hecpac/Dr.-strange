from __future__ import annotations

import tempfile
import unittest
from os import environ
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from claw_v2.adapters.anthropic import ClaudeSDKExecutor, create_claude_sdk_executor
from claw_v2.adapters.base import AdapterUnavailableError, LLMRequest
from claw_v2.llm import LLMRouter

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
                    yield FakeAssistantMessage([SimpleNamespace(text="ok")], "claude-opus-4-6")
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
                model="claude-opus-4-6",
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
            self.assertEqual(recorded["options"].kwargs["env"], {})
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
                model="claude-opus-4-6",
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


if __name__ == "__main__":
    unittest.main()
