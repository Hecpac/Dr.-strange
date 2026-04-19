from __future__ import annotations

import asyncio
import logging
import os
import re
from importlib import import_module
from pathlib import Path
from typing import Any, AsyncIterator, Callable

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
from claw_v2.tracing import trace_metadata
from claw_v2.types import LLMResponse

logger = logging.getLogger(__name__)

SILENCE_DIRECTIVE = (
    "\n\n# CRITICAL OUTPUT RULE:\n"
    "You are operating as a headless engine. DO NOT use conversational filler. "
    "DO NOT explain your thoughts, do not say 'I will now...', 'I have found...', "
    "or 'I am finished'.\n"
    "EVERY SINGLE WORD of your final response to the user MUST be wrapped inside <response> tags. "
    "Any text outside <response> tags will be discarded. "
    "Internal reasoning must go inside <trace> tags."
)


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
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, self._run(request)).result()
        return asyncio.run(self._run(request))

    async def _run(self, request: LLMRequest) -> LLMResponse:
        sdk = _load_sdk()
        stderr_lines: list[str] = []

        def _capture_stderr(line: str) -> None:
            stderr_lines.append(line)
            if len(stderr_lines) > 40:
                del stderr_lines[0]

        options = self._build_options(sdk, request, stderr_callback=_capture_stderr)
        effective_input = build_effective_input(request)
        assistant_text_chunks: list[str] = []
        result_text: str | None = None
        result_session_id = request.session_id
        total_cost = 0.0
        usage: dict[str, Any] = {}
        model_name = request.model
        query_session_id = request.session_id or "default"

        try:
            async with asyncio.timeout(request.timeout):
                async with sdk.ClaudeSDKClient(options=options) as client:
                    if isinstance(effective_input, str):
                        await client.query(effective_input, session_id=query_session_id)
                    else:
                        await client.query(
                            _stream_user_content(effective_input),
                            session_id=query_session_id,
                        )
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
        except TimeoutError as exc:  # pragma: no cover - exercised with fake SDK tests
            stderr_excerpt = " | ".join(stderr_lines[-5:]).strip()
            partial_text = _coalesce_content(assistant_text_chunks, result_text)
            self._emit_runtime_error(
                request=request,
                query_session_id=query_session_id,
                result_session_id=result_session_id,
                exc=exc,
                stderr_excerpt=stderr_excerpt,
                partial_text=partial_text,
            )
            raise AdapterError(
                f"Claude SDK execution timed out after {request.timeout:.1f}s"
            ) from exc
        except Exception as exc:  # pragma: no cover - runtime integration path
            stderr_excerpt = " | ".join(stderr_lines[-5:]).strip()
            self._emit_runtime_error(
                request=request,
                query_session_id=query_session_id,
                result_session_id=result_session_id,
                exc=exc,
                stderr_excerpt=stderr_excerpt,
                partial_text=_coalesce_content(assistant_text_chunks, result_text),
            )
            if exc.__class__.__name__ == "CLINotFoundError":
                raise AdapterUnavailableError(
                    f"Claude CLI not found at '{self.config.claude_cli_path}'."
                ) from exc
            if isinstance(exc, AdapterError):
                raise
            if stderr_excerpt:
                logger.error("Claude CLI stderr before failure: %s", stderr_excerpt)
                raise AdapterError(f"Claude SDK execution failed: {exc}. Claude stderr: {stderr_excerpt}") from exc
            raise AdapterError(f"Claude SDK execution failed: {exc}") from exc

        content = _coalesce_content(assistant_text_chunks, result_text)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_create = usage.get("cache_creation_input_tokens", 0)
        input_tokens = usage.get("input_tokens", 0)
        cache_hit_ratio = cache_read / max(input_tokens, 1) if input_tokens else 0.0
        artifacts = {
            "session_id": result_session_id,
            "usage": usage,
            "cache": {
                "read_tokens": cache_read,
                "create_tokens": cache_create,
                "hit_ratio": round(cache_hit_ratio, 3),
            },
        }
        if self.observe is not None:
            self.observe.emit(
                "prompt_cache",
                lane=request.lane,
                provider="anthropic",
                model=model_name,
                **trace_metadata(request.evidence_pack),
                payload={
                    "cache_read_tokens": cache_read,
                    "cache_create_tokens": cache_create,
                    "input_tokens": input_tokens,
                    "hit_ratio": round(cache_hit_ratio, 3),
                    "estimated_savings_pct": round(cache_hit_ratio * 75, 1),
                },
            )
        return LLMResponse(
            content=content,
            lane=request.lane,
            provider="anthropic",
            model=model_name,
            confidence=0.75 if content else 0.0,
            cost_estimate=total_cost,
            artifacts=artifacts,
        )

    def _emit_runtime_error(
        self,
        *,
        request: LLMRequest,
        query_session_id: str,
        result_session_id: str | None,
        exc: Exception,
        stderr_excerpt: str,
        partial_text: str,
    ) -> None:
        if self.observe is None:
            return
        payload = {
            "session_id": request.session_id,
            "query_session_id": query_session_id,
            "result_session_id": result_session_id,
            "error_type": exc.__class__.__name__,
            "error": str(exc)[:1000],
            "stderr_excerpt": stderr_excerpt[:1000],
            "partial_text_preview": partial_text[:500],
        }
        self.observe.emit(
            "llm_error",
            lane=request.lane,
            provider="anthropic",
            model=request.model,
            **trace_metadata(request.evidence_pack),
            payload=payload,
        )

    def _build_options(
        self,
        sdk: Any,
        request: LLMRequest,
        *,
        stderr_callback: Callable[[str], None] | None = None,
    ) -> Any:
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
                system_prompt["append"] = f"{effective_system_prompt}{SILENCE_DIRECTIVE}"
            else:
                system_prompt["append"] = SILENCE_DIRECTIVE
            permission_mode = "bypassPermissions" if self.config.sdk_bypass_permissions else "default"
            can_use_tool = self._build_can_use_tool(sdk, request)

        sdk_agents = self._build_agents(sdk, request)
        sdk_env: dict[str, str] = {}
        extra_args: dict[str, str | None] = {}
        if self._should_use_api_key_auth():
            if api_key := _resolve_anthropic_api_key():
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
            cli_path=self.config.claude_cli_path,
            cwd=Path(request.cwd) if request.cwd else self.config.workspace_root,
            # Isolate the bot from user-level Claude Code hooks/plugins.
            # The user's personal ~/.claude/settings.json can run heavy SessionStart
            # hooks or custom callbacks that break SDK initialization.
            setting_sources=["project", "local"],
            stderr=stderr_callback,
            extra_args=extra_args,
            env=sdk_env,
            hooks=hooks,
            agents=sdk_agents,
            can_use_tool=can_use_tool,
            effort=request.effort,
        )
        return sdk.ClaudeAgentOptions(**options_kwargs)

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
                    **trace_metadata(request.evidence_pack),
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
                    **trace_metadata(request.evidence_pack),
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
                    **trace_metadata(request.evidence_pack),
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
                    **trace_metadata(request.evidence_pack),
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
                    **trace_metadata(request.evidence_pack),
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
        extra_roots = getattr(self.config, "extra_workspace_roots", [])
        allowed = [workspace_root, *read_paths, *extra_roots, *getattr(self.config, "allowed_paths", [])]
        return SandboxPolicy(
            workspace_root=workspace_root,
            allowed_paths=allowed,
            writable_paths=[workspace_root, Path("/private/tmp"), Path.home() / ".claw", *extra_roots],
            network_policy="allow",
            credential_scope="external",
            capability_profile=getattr(self.config, "sandbox_capability_profile", "engineer"),
        )

    def _should_use_api_key_auth(self) -> bool:
        if self.config.claude_auth_mode == "api_key":
            return True
        if self.config.claude_auth_mode == "auto":
            return _resolve_anthropic_api_key() is not None
        return False


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


async def _stream_user_content(content: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }


def _coalesce_content(assistant_text_chunks: list[str], result_text: str | None) -> str:
    content = "\n".join(chunk.strip() for chunk in assistant_text_chunks if chunk.strip()).strip()
    if content:
        if result_text and result_text.strip() and result_text.strip() not in content:
            return f"{content}\n\n{result_text.strip()}"
        return content
    if result_text:
        return result_text.strip()
    return ""


def _resolve_anthropic_api_key() -> str | None:
    if value := os.getenv("ANTHROPIC_API_KEY"):
        return value.strip() or None
    pattern = re.compile(r"^\s*(?:export\s+)?ANTHROPIC_API_KEY=(?P<value>.+?)\s*$")
    for path in (
        Path.home() / ".zshrc",
        Path.home() / ".zprofile",
        Path.home() / ".zshenv",
        Path.home() / ".profile",
    ):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except FileNotFoundError:
            continue
        for line in reversed(lines):
            match = pattern.match(line)
            if match is None:
                continue
            value = match.group("value").strip().strip("\"'")
            if value:
                return value
    return None
