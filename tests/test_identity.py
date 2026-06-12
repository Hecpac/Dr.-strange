"""D3 — golden tests for the SDK system-prompt identity composition.

claw_v2.identity is the single origin of IDENTITY_OVERRIDE + SILENCE_DIRECTIVE
and of the composed block the Anthropic adapter appends to the claude_code
preset. These goldens are byte a byte on purpose: any drift in the persona
wiring must show up as a failing diff here, not as silent persona amnesia.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from claw_v2.adapters.anthropic import ClaudeSDKExecutor
from claw_v2.adapters.base import LLMRequest
from claw_v2.identity import (
    IDENTITY_OVERRIDE,
    SILENCE_DIRECTIVE,
    build_sdk_system_prompt_append,
)

from tests.helpers import make_config

GOLDEN_PERSONA = "You are Claw."

# Byte-for-byte golden of build_sdk_system_prompt_append(GOLDEN_PERSONA).
GOLDEN_APPEND = (
    "# IDENTITY OVERRIDE (HIGHEST PRIORITY)\n"
    "Your identity is Dr. Strange — Hector Pachano's autonomous personal agent. "
    "The Claude Code preset above describes your RUNTIME (the CLI you operate inside), "
    "NOT your identity. When the user asks who/what you are, what you do, or refers to "
    "Dr. Strange, you answer AS Dr. Strange — never as Claude, Claude Code, an AI assistant, "
    "or a generic agent. Dr. Strange is the persona; Claude/Claude Code is the underlying "
    "model and runtime. Never say 'I don't know what Dr. Strange is' or 'I am Claude/Claude Code' "
    "in user-facing chat. The persona definition that follows is canonical.\n"
    "\n"
    "You are Claw."
    "\n"
    "\n"
    "# CRITICAL OUTPUT RULE:\n"
    "You are operating as a headless engine. DO NOT use conversational filler. "
    "DO NOT explain your thoughts, do not say 'I will now...', 'I have found...', "
    "or 'I am finished'.\n"
    "EVERY SINGLE WORD of your final response to the user MUST be wrapped inside <response> tags. "
    "Any text outside <response> tags will be discarded. "
    "Internal reasoning must go inside <trace> tags."
)


class IdentityComposerGoldenTests(unittest.TestCase):
    def test_append_with_persona_matches_golden_byte_for_byte(self) -> None:
        self.assertEqual(build_sdk_system_prompt_append(GOLDEN_PERSONA), GOLDEN_APPEND)

    def test_append_without_persona_is_override_plus_silence(self) -> None:
        self.assertEqual(
            build_sdk_system_prompt_append(None),
            f"{IDENTITY_OVERRIDE}{SILENCE_DIRECTIVE}",
        )
        self.assertEqual(
            build_sdk_system_prompt_append(""),
            f"{IDENTITY_OVERRIDE}{SILENCE_DIRECTIVE}",
        )

    def test_golden_is_composed_from_the_canonical_parts(self) -> None:
        self.assertEqual(
            GOLDEN_APPEND,
            f"{IDENTITY_OVERRIDE}{GOLDEN_PERSONA}{SILENCE_DIRECTIVE}",
        )


class AdapterSystemPromptGoldenTests(unittest.TestCase):
    def test_executor_appends_exactly_the_composed_block(self) -> None:
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
                system_prompt=GOLDEN_PERSONA,
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

            self.assertEqual(options.kwargs["system_prompt"]["append"], GOLDEN_APPEND)


if __name__ == "__main__":
    unittest.main()
