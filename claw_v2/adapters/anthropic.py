from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import Any, Callable

from claw_v2.adapters.base import (
    ADVISORY_LANES,
    AdapterError,
    AdapterUnavailableError,
    LLMRequest,
    ProviderAdapter,
    build_effective_input,
    build_effective_system_prompt,
    coerce_usage_dict,
)
from claw_v2.approval import ApprovalManager
from claw_v2.config import AppConfig
from claw_v2.network_proxy import DomainAllowlistEnforcer
from claw_v2.observe import ObserveStream
from claw_v2.sandbox import SandboxPolicy, sandbox_hook
from claw_v2.types import LLMResponse


class AnthropicAgentAdapter(ProviderAdapter):
    provider_name = "anthropic"
    tool_capable = True

    def __init__(self, executor: Callable[[LLMRequest], LLMResponse] | None = None) -> None:
        self._executor = executor

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._executor is None:
            raise AdapterUnavailableError(
                "Anthropic adapter requires a Claude SDK executor in this environment."
            )
        return self._executor(request)


class ClaudeSDKExecutor:
    def __init__(
        self,
        config: AppConfig,
        *,
        observe: ObserveStream | None = None,
        approvals: ApprovalManager | None = None,
    ) -> None:
        self.config = config
        self.observe = observe
        self.approvals = approvals
        self.network_enforcer = DomainAllowlistEnforcer()

    def __call__(self, request: LLMRequest) -> LLMResponse:
        with self._auth_environment():
            return asyncio.run(self._run(request))

    async def _run(self, request: LLMRequest) -> LLMResponse:
        sdk = _load_sdk()
        options = self._build_options(sdk, request)
        effective_input = build_effective_input(request)
        assistant_text_chunks: list[str] = []
        result_text: str | None = None
        result_session_id = request.session_id
        total_cost = 0.0
        usage: dict[str, Any] = {}
        model_name = request.model

        try:
            async with sdk.ClaudeSDKClient(options=options) as client:
                await client.query(effective_input)
                async for message in client.receive_response():
                    if isinstance(message, sdk.AssistantMessage):
                        assistant_text_chunks.extend(_extract_assistant_text(message.content))
                        model_name = getattr(message, "model", model_name) or model_name
                    elif isinstance(message, sdk.ResultMessage):
                        result_session_id = message.session_id or result_session_id
                        total_cost = float(message.total_cost_usd or 0.0)
                        usage = coerce_usage_dict(message.usage)
                        result_text = message.result
                        if message.is_error:
                            raise AdapterError(message.result or "Claude SDK execution failed.")
        except Exception as exc:  # pragma: no cover - runtime integration path
            if exc.__class__.__name__ == "CLINotFoundError":
                raise AdapterUnavailableError(
                    f"Claude CLI not found at '{self.config.claude_cli_path}'."
                ) from exc
            if isinstance(exc, AdapterError):
                raise
            raise AdapterError(f"Claude SDK execution failed: {exc}") from exc

        content = _coalesce_content(assistant_text_chunks, result_text)
        artifacts = {
            "session_id": result_session_id,
            "usage": usage,
        }
        return LLMResponse(
            content=content,
            lane=request.lane,
            provider="anthropic",
            model=model_name,
            confidence=0.75 if content else 0.0,
            cost_estimate=total_cost,
            artifacts=artifacts,
        )

    def _build_options(self, sdk: Any, request: LLMRequest) -> Any:
        tools: Any
        system_prompt: Any
        can_use_tool = None
        hooks = self._build_hooks(sdk, request)
        effective_system_prompt = build_effective_system_prompt(request)

        if request.lane in ADVISORY_LANES:
            tools = []
            system_prompt = effective_system_prompt
            permission_mode = "plan"
        else:
            tools = {"type": "preset", "preset": "claude_code"}
            system_prompt = {"type": "preset", "preset": "claude_code"}
            if effective_system_prompt:
                system_prompt["append"] = effective_system_prompt
            permission_mode = "default"
            can_use_tool = self._build_can_use_tool(sdk, request)

        sdk_agents = self._build_agents(sdk, request)
        return sdk.ClaudeAgentOptions(
            tools=tools,
            allowed_tools=list(request.allowed_tools or []),
            system_prompt=system_prompt,
            permission_mode=permission_mode,
            resume=request.session_id,
            max_budget_usd=request.max_budget,
            model=request.model,
            cli_path=self.config.claude_cli_path,
            cwd=Path(request.cwd) if request.cwd else self.config.workspace_root,
            setting_sources=["project"],
            hooks=hooks,
            agents=sdk_agents,
            can_use_tool=can_use_tool,
            effort=request.effort,
        )

    def _build_agents(self, sdk: Any, request: LLMRequest) -> dict[str, Any] | None:
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

    def _build_can_use_tool(self, sdk: Any, request: LLMRequest) -> Callable[..., Any]:
        allowed = set(request.allowed_tools or [])
        policy = self._policy_for_request(request)
        sdk_types = _load_sdk_types()

        async def can_use_tool(tool_name: str, input_data: dict[str, Any], context: Any) -> Any:
            if allowed and tool_name not in allowed:
                return sdk_types.PermissionResultDeny(
                    message=f"Tool '{tool_name}' is not allowed in this execution.",
                    interrupt=True,
                )

            decision = sandbox_hook(
                tool_name,
                input_data,
                policy=policy,
                network_enforcer=self.network_enforcer,
                actor=request.lane,
            )
            if not decision.allowed:
                return sdk_types.PermissionResultDeny(message=decision.reason, interrupt=True)
            return sdk_types.PermissionResultAllow(updated_input=input_data)

        return can_use_tool

    def _build_hooks(self, sdk: Any, request: LLMRequest) -> dict[str, list[Any]] | None:
        hooks: dict[str, list[Any]] = {}
        policy = self._policy_for_request(request)

        async def pre_tool_use(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
            decision = sandbox_hook(
                input_data.get("tool_name", ""),
                input_data.get("tool_input", {}),
                policy=policy,
                network_enforcer=self.network_enforcer,
                actor=request.lane,
            )
            if not decision.allowed:
                return {
                    "systemMessage": f"Tool invocation blocked: {decision.reason}",
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": decision.reason,
                    },
                }
            return {"continue_": True}

        async def post_tool_use(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
            if self.observe is not None:
                self.observe.emit(
                    "sdk_post_tool_use",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                    payload={
                        "tool_name": input_data.get("tool_name"),
                        "tool_use_id": tool_use_id,
                        "session_id": input_data.get("session_id"),
                    },
                )
            return {}

        async def post_tool_use_failure(
            input_data: dict[str, Any], tool_use_id: str | None, context: Any
        ) -> dict[str, Any]:
            if self.observe is not None:
                self.observe.emit(
                    "sdk_post_tool_use_failure",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                    payload={
                        "tool_name": input_data.get("tool_name"),
                        "tool_use_id": tool_use_id,
                        "session_id": input_data.get("session_id"),
                    },
                )
            return {}

        async def stop_hook(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
            if self.observe is not None:
                self.observe.emit(
                    "sdk_stop",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                    payload={"session_id": input_data.get("session_id")},
                )
            return {}

        async def subagent_start(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
            if self.observe is not None:
                self.observe.emit(
                    "sdk_subagent_start",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                    payload={"agent_id": input_data.get("agent_id"), "tool_use_id": tool_use_id},
                )
            return {}

        async def subagent_stop(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
            if self.observe is not None:
                self.observe.emit(
                    "sdk_subagent_stop",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                    payload={"agent_id": input_data.get("agent_id"), "tool_use_id": tool_use_id},
                )
            return {}

        hooks["Stop"] = [sdk.HookMatcher(hooks=[stop_hook])]
        hooks["SubagentStart"] = [sdk.HookMatcher(hooks=[subagent_start])]
        hooks["SubagentStop"] = [sdk.HookMatcher(hooks=[subagent_stop])]

        if request.lane not in ADVISORY_LANES:
            hooks["PreToolUse"] = [sdk.HookMatcher(hooks=[pre_tool_use])]
            hooks["PostToolUse"] = [sdk.HookMatcher(hooks=[post_tool_use])]
            hooks["PostToolUseFailure"] = [sdk.HookMatcher(hooks=[post_tool_use_failure])]

        if request.hooks:
            for event_name, matchers in request.hooks.items():
                hooks.setdefault(event_name, []).extend(matchers)
        return hooks

    def _policy_for_request(self, request: LLMRequest) -> SandboxPolicy:
        workspace_root = Path(request.cwd) if request.cwd else self.config.workspace_root
        read_paths = getattr(self.config, "allowed_read_paths", [])
        return SandboxPolicy(
            workspace_root=workspace_root,
            allowed_paths=[workspace_root, *read_paths],
            writable_paths=[workspace_root],
            network_policy="allow",
            credential_scope="external",
        )

    @contextmanager
    def _auth_environment(self):
        if self.config.claude_auth_mode != "subscription":
            yield
            return
        original_api_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            yield
        finally:
            if original_api_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = original_api_key


def create_claude_sdk_executor(
    config: AppConfig,
    *,
    observe: ObserveStream | None = None,
    approvals: ApprovalManager | None = None,
) -> Callable[[LLMRequest], LLMResponse]:
    executor = ClaudeSDKExecutor(config, observe=observe, approvals=approvals)
    return executor


def _load_sdk() -> Any:
    try:
        return import_module("claude_agent_sdk")
    except ModuleNotFoundError as exc:
        raise AdapterUnavailableError(
            "claude_agent_sdk is not installed. Install the 'claude-agent-sdk' Python package to enable Anthropic runtime support."
        ) from exc


def _load_sdk_types() -> Any:
    try:
        return import_module("claude_agent_sdk.types")
    except ModuleNotFoundError as exc:
        raise AdapterUnavailableError(
            "claude_agent_sdk is not installed. Install the 'claude-agent-sdk' Python package to enable Anthropic runtime support."
        ) from exc


def _extract_assistant_text(blocks: list[Any]) -> list[str]:
    chunks: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            chunks.append(text)
    return chunks


def _coalesce_content(assistant_text_chunks: list[str], result_text: str | None) -> str:
    content = "\n".join(chunk.strip() for chunk in assistant_text_chunks if chunk.strip()).strip()
    if content:
        if result_text and result_text.strip() and result_text.strip() not in content:
            return f"{content}\n\n{result_text.strip()}"
        return content
    if result_text:
        return result_text.strip()
    return ""
