"""ClaudeAgentOptions assembly for the Claude SDK executor.

Split out of anthropic.py (D1, 2026-06-12). Pure move: system-prompt/persona
wiring, sub-agent definitions, auth env, and the brain-lane delegation MCP
server must behave exactly as the executor methods they replace.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable

from claw_v2.adapters.anthropic_auth import (
    resolve_anthropic_api_key,
    should_use_api_key_auth,
)
from claw_v2.adapters.base import (
    ADVISORY_LANES,
    LLMRequest,
    build_effective_system_prompt,
)
from claw_v2.config import AppConfig

logger = logging.getLogger(__name__)

IDENTITY_OVERRIDE = (
    "# IDENTITY OVERRIDE (HIGHEST PRIORITY)\n"
    "Your identity is Dr. Strange — Hector Pachano's autonomous personal agent. "
    "The Claude Code preset above describes your RUNTIME (the CLI you operate inside), "
    "NOT your identity. When the user asks who/what you are, what you do, or refers to "
    "Dr. Strange, you answer AS Dr. Strange — never as Claude, Claude Code, an AI assistant, "
    "or a generic agent. Dr. Strange is the persona; Claude/Claude Code is the underlying "
    "model and runtime. Never say 'I don't know what Dr. Strange is' or 'I am Claude/Claude Code' "
    "in user-facing chat. The persona definition that follows is canonical.\n\n"
)

SILENCE_DIRECTIVE = (
    "\n\n# CRITICAL OUTPUT RULE:\n"
    "You are operating as a headless engine. DO NOT use conversational filler. "
    "DO NOT explain your thoughts, do not say 'I will now...', 'I have found...', "
    "or 'I am finished'.\n"
    "EVERY SINGLE WORD of your final response to the user MUST be wrapped inside <response> tags. "
    "Any text outside <response> tags will be discarded. "
    "Internal reasoning must go inside <trace> tags."
)

# Modes accepted by the brain's delegate_task tool. Mirrors what
# planned_phases_for_mode + _build_coordinator_tasks can execute.
_DELEGATE_TASK_MODES = frozenset({"coding", "research", "ops", "publish", "browse"})


def build_delegation_mcp_server(sdk: Any, request: LLMRequest) -> Any:
    """In-process MCP server exposing `delegate_task` to brain-lane turns.

    The handler only enqueues a durable autonomous task (TaskHandler side);
    it must never run the delegated work itself. Enforcement of the tool
    name still goes through runtime_policy via the PreToolUse hook, so the
    `mcp__claw__delegate_task` policy entry is load-bearing.
    """
    handler = request.delegation_handler

    @sdk.tool(
        "delegate_task",
        (
            "Delegate long-running work (GUI/computer-use, browser sessions, "
            "publishing, multi-step jobs) to the durable autonomous-task lane. "
            "Returns an acknowledgement with the task id; the result is "
            "delivered to the user when the task finishes."
        ),
        {
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "description": "Imperative, self-contained objective for the task.",
                },
                "mode": {
                    "type": "string",
                    "enum": sorted(_DELEGATE_TASK_MODES),
                    "description": "Execution mode; omit to infer from the objective.",
                },
                "reason": {
                    "type": "string",
                    "description": "One line on why this work is being delegated.",
                },
            },
            "required": ["objective"],
        },
    )
    async def delegate_task(args: dict[str, Any]) -> dict[str, Any]:
        def _error(text: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": text}], "is_error": True}

        objective = args.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            return _error("delegate_task error: objective must be a non-empty string")
        mode = args.get("mode")
        if mode is not None and mode not in _DELEGATE_TASK_MODES:
            return _error(
                "delegate_task error: mode must be one of "
                + ", ".join(sorted(_DELEGATE_TASK_MODES))
            )
        payload = {
            "objective": objective.strip(),
            "mode": mode,
            "reason": str(args.get("reason") or "")[:300],
        }
        try:
            result = await asyncio.to_thread(handler, payload)
        except Exception as exc:
            logger.exception("delegate_task handler failed")
            return _error(f"delegate_task error: {str(exc)[:300]}")
        ack = str((result or {}).get("ack") or "").strip()
        if not ack:
            return _error("delegate_task error: delegation returned no acknowledgement")
        return {"content": [{"type": "text", "text": ack}]}

    return sdk.create_sdk_mcp_server(
        name="claw", version="1.0.0", tools=[delegate_task]
    )


def build_agents(sdk: Any, request: LLMRequest) -> dict[str, Any] | None:
    if not request.agents:
        return None
    built: dict[str, Any] = {}
    for name, raw in request.agents.items():
        if isinstance(raw, dict):
            if {"description", "prompt"} <= set(raw):
                built[name] = sdk.AgentDefinition(
                    description=raw["description"],
                    prompt=raw["prompt"],
                    tools=raw.get("tools"),
                    model=raw.get("model"),
                )
                continue
            built[name] = sdk.AgentDefinition(
                description=raw.get("agent_class", name),
                prompt=raw.get("instruction", ""),
                tools=raw.get("allowed_tools"),
                model=raw.get("model", "inherit"),
            )
            continue
        built[name] = sdk.AgentDefinition(
            description=getattr(raw, "agent_class", name),
            prompt=getattr(raw, "instruction", ""),
            tools=getattr(raw, "allowed_tools", None),
            model=getattr(raw, "model", "inherit"),
        )
    return built


def build_options(
    sdk: Any,
    request: LLMRequest,
    *,
    config: AppConfig,
    hooks: dict[str, list[Any]] | None,
    can_use_tool: Callable[..., Any] | None,
    stderr_callback: Callable[[str], None] | None = None,
) -> Any:
    tools: Any
    system_prompt: Any
    effective_system_prompt = build_effective_system_prompt(request)

    if request.lane in ADVISORY_LANES:
        tools = []
        system_prompt = effective_system_prompt
        permission_mode = "plan"
    else:
        tools = {"type": "preset", "preset": "claude_code"}
        system_prompt = {"type": "preset", "preset": "claude_code"}
        # Prepend an identity-override block so the Dr. Strange persona wins
        # over the Claude Code preset's default "I am Claude" identity when
        # the user asks identity-style questions.
        if effective_system_prompt:
            system_prompt["append"] = (
                f"{IDENTITY_OVERRIDE}{effective_system_prompt}{SILENCE_DIRECTIVE}"
            )
        else:
            system_prompt["append"] = f"{IDENTITY_OVERRIDE}{SILENCE_DIRECTIVE}"
        permission_mode = (
            "bypassPermissions" if config.sdk_bypass_permissions else "default"
        )

    sdk_agents = build_agents(sdk, request)
    sdk_env: dict[str, str] = {}
    extra_args: dict[str, str | None] = {"disable-slash-commands": None}
    if should_use_api_key_auth(config):
        if api_key := resolve_anthropic_api_key():
            sdk_env["ANTHROPIC_API_KEY"] = api_key
        extra_args["bare"] = None
    elif os.environ.get("ANTHROPIC_API_KEY"):
        sdk_env["ANTHROPIC_API_KEY"] = ""
    options_kwargs: dict[str, Any] = dict(
        tools=tools,
        allowed_tools=list(request.allowed_tools or []),
        system_prompt=system_prompt,
        permission_mode=permission_mode,
        resume=request.session_id,
        max_budget_usd=request.max_budget,
        model=request.model,
        cli_path=config.claude_cli_path,
        cwd=Path(request.cwd) if request.cwd else config.workspace_root,
        # Isolate the bot from Claude Code user/project/local settings and
        # skills. Telegram policy/sandbox is the source of truth here.
        setting_sources=[],
        stderr=stderr_callback,
        extra_args=extra_args,
        env=sdk_env,
        hooks=hooks,
        agents=sdk_agents,
        can_use_tool=can_use_tool,
        effort=request.effort,
    )
    if request.thinking_tokens > 0:
        options_kwargs["thinking"] = {
            "type": "enabled",
            "budget_tokens": int(request.thinking_tokens),
        }
        options_kwargs["max_thinking_tokens"] = int(request.thinking_tokens)
    if request.lane == "brain" and request.delegation_handler is not None:
        options_kwargs["mcp_servers"] = {
            "claw": build_delegation_mcp_server(sdk, request)
        }
    return sdk.ClaudeAgentOptions(**options_kwargs)
