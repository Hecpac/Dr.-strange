"""Anthropic adapter + Claude SDK executor (turn flow).

D1 split (2026-06-12): hooks live in anthropic_hooks.py, options assembly in
anthropic_options.py, API-key resolution in anthropic_auth.py. This module
keeps the adapter, the executor's turn flow (`_run`), and the SDK loaders.
"""

from __future__ import annotations

import asyncio
import logging
from importlib import import_module
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from claw_v2.adapters.anthropic_hooks import (
    _inline_browser_drive_reason as _inline_browser_drive_reason,
    _safe_runtime_policy_reason as _safe_runtime_policy_reason,
    _tool_input_evidence as _tool_input_evidence,
    _tool_response_evidence as _tool_response_evidence,
    build_can_use_tool,
    build_hooks,
)
from claw_v2.adapters.anthropic_options import (
    IDENTITY_OVERRIDE as IDENTITY_OVERRIDE,
    SILENCE_DIRECTIVE as SILENCE_DIRECTIVE,
    build_options,
)
from claw_v2.adapters.base import (
    ADVISORY_LANES,
    AdapterError,
    AdapterUnavailableError,
    LLMRequest,
    ProviderAdapter,
    build_effective_input,
    coerce_usage_dict,
    record_tools_executed,
)
from claw_v2.approval import ApprovalManager
from claw_v2.config import AppConfig
from claw_v2.network_proxy import DomainAllowlistEnforcer
from claw_v2.observe import ObserveStream
from claw_v2.approval_gate import build_telegram_approval_gate
from claw_v2.runtime_policy import RuntimePolicyEngine
from claw_v2.sandbox import SandboxPolicy
from claw_v2.tracing import trace_metadata
from claw_v2.types import LLMResponse

logger = logging.getLogger(__name__)


class AnthropicAgentAdapter(ProviderAdapter):
    provider_name = "anthropic"
    tool_capable = True

    def __init__(
        self, executor: Callable[[LLMRequest], LLMResponse] | None = None
    ) -> None:
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

        mutating_tools: list[str] = []
        options = self._build_options(
            sdk,
            request,
            stderr_callback=_capture_stderr,
            mutation_tracker=mutating_tools,
        )
        effective_input = build_effective_input(request)
        assistant_text_chunks: list[str] = []
        result_text: str | None = None
        result_session_id = request.session_id
        total_cost = 0.0
        usage: dict[str, Any] = {}
        model_name = request.model
        query_session_id = request.session_id or "default"

        async def _consume_turn() -> None:
            nonlocal result_text, result_session_id, total_cost, usage, model_name
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
                        assistant_text_chunks.extend(
                            _extract_assistant_text(message.content)
                        )
                        model_name = getattr(message, "model", model_name) or model_name
                    elif isinstance(message, sdk.ResultMessage):
                        result_session_id = message.session_id or result_session_id
                        total_cost = float(message.total_cost_usd or 0.0)
                        usage = coerce_usage_dict(message.usage)
                        result_text = message.result
                        if message.is_error:
                            stderr_hint = " | ".join(stderr_lines[-5:]).strip()
                            detail = message.result or "Claude SDK execution failed."
                            if stderr_hint:
                                detail = f"{detail} (stderr: {stderr_hint})"
                            raise AdapterError(detail)

        try:
            try:
                # request.timeout was validated and policy-checked but never
                # enforced here, so a hung provider call blocked the worker
                # thread indefinitely. reason="timeout" feeds the router's
                # llm_timeout audit and the provider circuit breaker.
                await asyncio.wait_for(_consume_turn(), timeout=request.timeout)
            except TimeoutError as timeout_exc:
                raise AdapterError(
                    f"Claude SDK execution timed out after {request.timeout:.0f}s",
                    metadata={"reason": "timeout", "timeout_seconds": request.timeout},
                ) from timeout_exc
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
                record_tools_executed(exc, mutating_tools)
                raise
            if stderr_excerpt:
                logger.error("Claude CLI stderr before failure: %s", stderr_excerpt)
                error = AdapterError(
                    f"Claude SDK execution failed: {exc}. Claude stderr: {stderr_excerpt}"
                )
                record_tools_executed(error, mutating_tools)
                raise error from exc
            error = AdapterError(f"Claude SDK execution failed: {exc}")
            record_tools_executed(error, mutating_tools)
            raise error from exc

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
        mutation_tracker: list[str] | None = None,
    ) -> Any:
        hooks = build_hooks(
            sdk,
            request,
            runtime_policy=self._runtime_policy_for_request(
                request, self._policy_for_request(request)
            ),
            observe=self.observe,
            mutation_tracker=mutation_tracker,
        )
        can_use_tool = None
        if request.lane not in ADVISORY_LANES:
            can_use_tool = build_can_use_tool(
                _load_sdk_types(),
                request,
                runtime_policy=self._runtime_policy_for_request(
                    request, self._policy_for_request(request)
                ),
            )
        return build_options(
            sdk,
            request,
            config=self.config,
            hooks=hooks,
            can_use_tool=can_use_tool,
            stderr_callback=stderr_callback,
        )

    def _policy_for_request(self, request: LLMRequest) -> SandboxPolicy:
        workspace_root = (
            Path(request.cwd) if request.cwd else self.config.workspace_root
        )
        read_paths = getattr(self.config, "allowed_read_paths", [])
        extra_roots = getattr(self.config, "extra_workspace_roots", [])
        allowed = [
            workspace_root,
            *read_paths,
            *extra_roots,
            *getattr(self.config, "allowed_paths", []),
        ]
        return SandboxPolicy(
            workspace_root=workspace_root,
            allowed_paths=allowed,
            writable_paths=[
                workspace_root,
                Path("/private/tmp"),
                Path.home() / ".claw",
                *extra_roots,
            ],
            network_policy="allow",
            credential_scope="external",
            capability_profile=getattr(
                self.config, "sandbox_capability_profile", "engineer"
            ),
        )

    def _runtime_policy_for_request(
        self, request: LLMRequest, policy: SandboxPolicy
    ) -> RuntimePolicyEngine:
        approval_gate = (
            build_telegram_approval_gate(self.approvals)
            if self.approvals is not None
            else None
        )
        return RuntimePolicyEngine(
            workspace_root=policy.workspace_root,
            sandbox_policy=policy,
            network_enforcer=self.network_enforcer,
            approval_gate=approval_gate,
            autoexec_max_tier=getattr(self.config, "tier_autoexec_max", 2),
        )


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


async def _stream_user_content(
    content: list[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }


def _coalesce_content(assistant_text_chunks: list[str], result_text: str | None) -> str:
    content = "\n".join(
        chunk.strip() for chunk in assistant_text_chunks if chunk.strip()
    ).strip()
    if content:
        if result_text and result_text.strip() and result_text.strip() not in content:
            return f"{content}\n\n{result_text.strip()}"
        return content
    if result_text:
        return result_text.strip()
    return ""
