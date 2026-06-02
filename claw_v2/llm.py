from __future__ import annotations

from dataclasses import asdict
import re
from typing import Callable

from claw_v2.adapters.anthropic import AnthropicAgentAdapter
from claw_v2.adapters.base import (
    ADVISORY_EVIDENCE_PACK_MAX_CHARS,
    ADVISORY_LANES,
    AdapterError,
    AdapterUnavailableError,
    LLMRequest,
    PostLLMHook,
    PreLLMHook,
    ProviderAdapter,
    UserPrompt,
    build_effective_input,
    build_effective_system_prompt,
    evidence_pack_serialized_chars,
)
from claw_v2.adapters.codex import CodexAdapter
from claw_v2.adapters.google import GoogleAdapter
from claw_v2.adapters.ollama import OllamaAdapter
from claw_v2.adapters.openai import OpenAIAdapter
from claw_v2.config import AppConfig
from claw_v2.redaction import redact_sensitive
from claw_v2.retry_policy import ProviderCircuitBreaker
from claw_v2.tracing import current_llm_trace, trace_metadata
from claw_v2.types import Lane, LLMResponse


class LLMRouter:
    """Multi-lane router with explicit fallback for API/local providers."""

    NON_TOOL_LANES: tuple[Lane, ...] = ("verifier", "research", "judge")

    def __init__(
        self,
        config: AppConfig,
        adapters: dict[str, ProviderAdapter],
        audit_sink: Callable[[dict], None] | None = None,
        pre_hooks: list[PreLLMHook] | None = None,
        post_hooks: list[PostLLMHook] | None = None,
        circuit_breaker: ProviderCircuitBreaker | None = None,
        observation_window: object | None = None,
    ) -> None:
        self.config = config
        self.adapters = adapters
        self.audit_sink = audit_sink or (lambda event: None)
        self.pre_hooks: list[PreLLMHook] = list(pre_hooks or [])
        self.post_hooks: list[PostLLMHook] = list(post_hooks or [])
        self.circuit_breaker = circuit_breaker or ProviderCircuitBreaker()
        self.observation_window = observation_window

    def ask(
        self,
        prompt: UserPrompt,
        *,
        system_prompt: str | None = None,
        lane: Lane = "brain",
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        session_id: str | None = None,
        max_budget: float | None = None,
        evidence_pack: dict | None = None,
        allowed_tools: list[str] | None = None,
        agents: dict | None = None,
        hooks: dict | None = None,
        cwd: str | None = None,
        timeout: float = 300.0,
        thinking_tokens: int | None = None,
    ) -> LLMResponse:
        self._validate_lane_input(lane, evidence_pack, allowed_tools, agents, hooks)
        configured_provider = self.config.provider_for_lane(lane)
        selected_provider = provider or configured_provider
        selected_model = (
            model
            or (
                self.config.advisory_model_for_provider(selected_provider)
                if provider and selected_provider != configured_provider
                else self.config.model_for_lane(lane)
            )
        )
        _validate_provider_model_pair(selected_provider, selected_model)
        selected_effort = effort or self.config.effort_for_lane(lane)
        selected_thinking = (
            thinking_tokens
            if thinking_tokens is not None
            else self.config.thinking_tokens_for_lane(lane)
        )
        requested_budget = max_budget if max_budget is not None else self.config.max_budget_usd
        budget = self.config.effective_max_budget_for_request(
            lane=lane,
            provider=selected_provider,
            requested_budget=requested_budget,
        )
        request = LLMRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            lane=lane,
            provider=selected_provider,
            model=selected_model,
            effort=selected_effort,
            session_id=session_id,
            max_budget=budget,
            evidence_pack=evidence_pack,
            allowed_tools=allowed_tools,
            agents=agents,
            hooks=hooks,
            timeout=timeout,
            cwd=cwd,
            cache_ttl=self.config.cache_prefix_ttl if self.config.cache_prefix_ttl > 0 else None,
            thinking_tokens=max(0, int(selected_thinking)),
        )
        request.evidence_pack = {
            **(request.evidence_pack or {}),
            **current_llm_trace(request.evidence_pack),
        }
        request.validate()
        # --- Pre-hooks ---
        for hook in self.pre_hooks:
            result = hook(request)
            if result is None:
                hook_name = getattr(hook, "__name__", "pre_hook")
                block_reason = getattr(hook, "block_reason", None) or "no_reason_provided"
                content = f"Request blocked by pre-hook ({hook_name}). Reason: {block_reason}"
                blocked = LLMResponse(
                    content=content,
                    lane=request.lane,
                    provider="none",
                    model="none",
                    confidence=0.0,
                    cost_estimate=0.0,
                    artifacts={"blocked_by": hook_name, "block_reason": block_reason},
                )
                self._audit(
                    "llm_pre_hook_blocked",
                    blocked,
                    {
                        "blocked_by": hook_name,
                        "block_reason": block_reason,
                        "session_id": session_id,
                        "prompt_size": _prompt_size_metadata(request),
                    },
                    request=request,
                )
                return blocked
            request = result
            request.validate()
            _validate_provider_model_pair(request.provider, request.model)

        adapter = self._adapter_for(request.provider)
        if lane not in self.NON_TOOL_LANES and not adapter.tool_capable:
            raise ValueError(f"Lane '{lane}' requires a tool-capable provider adapter.")

        self._observation_before_llm_request(request)

        try:
            response = self._complete_with_circuit(adapter, request)
            _suppress_corrupt_provider_content(response)
        except AdapterError as exc:
            fallback_provider = self._pick_fallback(request.provider, lane)
            if fallback_provider is None:
                raise
            fb_adapter = self._adapter_for(fallback_provider)
            if lane not in self.NON_TOOL_LANES and not fb_adapter.tool_capable:
                raise
            fallback_request = LLMRequest(
                **{
                    **asdict(request),
                    "provider": fallback_provider,
                    "model": self.config.advisory_model_for_provider(fallback_provider),
                    "session_id": _fallback_session_id(request, fallback_provider),
                }
            )
            _validate_provider_model_pair(fallback_request.provider, fallback_request.model)
            fallback_request.validate()
            self._observation_before_llm_request(fallback_request)
            response = self._complete_with_circuit(fb_adapter, fallback_request)
            _suppress_corrupt_provider_content(response)
            response.degraded_mode = True
            response.artifacts["fallback_reason"] = redact_sensitive(str(exc))
            response.artifacts.update(trace_metadata(fallback_request.evidence_pack))
            for hook in self.post_hooks:
                response = hook(fallback_request, response)
            self._audit(
                "llm_fallback",
                response,
                {
                    "requested_provider": selected_provider,
                    "fallback_provider": fallback_provider,
                    "response_text": redact_sensitive(response.content),
                    "prompt_size": _prompt_size_metadata(fallback_request),
                },
                request=fallback_request,
            )
            return response

        # --- Post-hooks ---
        for hook in self.post_hooks:
            response = hook(request, response)

        response.artifacts.update(trace_metadata(request.evidence_pack))
        self._audit(
            "llm_response",
            response,
            {
                "session_id": session_id,
                "response_text": redact_sensitive(response.content),
                "prompt_size": _prompt_size_metadata(request),
            },
            request=request,
        )
        if response.cost_unknown:
            # Billable provider returned a model with no price entry: surface it
            # loudly so the cost gate can fail closed (2026-05-31 audit H5).
            self._audit_event(
                "cost_metering_unknown",
                request=request,
                metadata={"provider": response.provider, "model": response.model},
            )
        return response

    def _complete_with_circuit(self, adapter: ProviderAdapter, request: LLMRequest) -> LLMResponse:
        decision = self.circuit_breaker.check(request.provider)
        if not decision.allowed:
            self._audit_event(
                "llm_circuit_blocked",
                request=request,
                metadata={
                    "provider": request.provider,
                    "failures": decision.failures,
                    "opened_until": decision.opened_until,
                    "reason": decision.reason,
                },
            )
            raise AdapterUnavailableError(
                f"Provider circuit open for '{request.provider}' until {decision.opened_until:.0f}: {decision.reason}"
            )
        try:
            response = adapter.complete(request)
        except AdapterError as exc:
            transition = self.circuit_breaker.record_failure(request.provider, exc)
            if transition.status == "open" and transition.changed:
                self._audit_event(
                    "llm_circuit_open",
                    request=request,
                    metadata={
                        "provider": request.provider,
                        "failures": transition.failures,
                        "opened_until": transition.opened_until,
                        "reason": transition.reason,
                    },
                )
            raise
        transition = self.circuit_breaker.record_success(request.provider)
        if transition.changed:
            self._audit_event(
                "llm_circuit_recovered",
                request=request,
                metadata={"provider": request.provider},
            )
        return response

    # Fallback order: anthropic ↔ openai; advisory-only providers fall back to Anthropic.
    # Codex is a ChatGPT subscription runtime and must not silently degrade to Claude.
    _FALLBACK_MAP: dict[str, str] = {
        "anthropic": "openai",
        "openai": "anthropic",
    }

    def _pick_fallback(self, failed_provider: str, lane: Lane) -> str | None:
        if failed_provider == "codex":
            return None
        candidate = self._FALLBACK_MAP.get(failed_provider)
        if candidate and candidate in self.adapters:
            return candidate
        if lane in self.NON_TOOL_LANES and failed_provider != "anthropic" and "anthropic" in self.adapters:
            return "anthropic"
        return None

    def _adapter_for(self, provider: str) -> ProviderAdapter:
        if provider not in self.adapters:
            raise AdapterError(f"No adapter registered for provider '{provider}'.")
        return self.adapters[provider]

    def _audit(self, action: str, response: LLMResponse, metadata: dict, *, request: LLMRequest | None = None) -> None:
        trace = (request.evidence_pack or {}) if request is not None else {}
        token_usage = _token_usage_metadata(response, request, metadata)
        event = {
            "action": action,
            "lane": response.lane,
            "provider": response.provider,
            "model": response.model,
            "cost_estimate": response.cost_estimate,
            "cost_unknown": response.cost_unknown,
            "confidence": response.confidence,
            "degraded_mode": response.degraded_mode,
            "metadata": {
                **metadata,
                "token_usage": token_usage,
                "trace_id": trace.get("trace_id"),
                "root_trace_id": trace.get("root_trace_id"),
                "span_id": trace.get("span_id"),
                "parent_span_id": trace.get("parent_span_id"),
                "job_id": trace.get("job_id"),
                "artifact_id": trace.get("artifact_id"),
            },
        }
        self.audit_sink(event)
        self._observation_audit(event)

    def _observation_before_llm_request(self, request: LLMRequest) -> None:
        if self.observation_window is None:
            return
        handler = getattr(self.observation_window, "before_llm_request", None)
        if handler is None:
            return
        try:
            handler(
                lane=request.lane,
                provider=request.provider,
                model=request.model,
                estimated_input_tokens=_prompt_size_metadata(request)["estimated_total_input_tokens"],
            )
        except PermissionError:
            raise
        except Exception:
            return

    def _audit_event(self, action: str, *, request: LLMRequest, metadata: dict) -> None:
        trace = request.evidence_pack or {}
        event = {
            "action": action,
            "lane": request.lane,
            "provider": request.provider,
            "model": request.model,
            "cost_estimate": 0.0,
            "confidence": 0.0,
            "degraded_mode": False,
            "metadata": {
                **metadata,
                "trace_id": trace.get("trace_id"),
                "root_trace_id": trace.get("root_trace_id"),
                "span_id": trace.get("span_id"),
                "parent_span_id": trace.get("parent_span_id"),
                "job_id": trace.get("job_id"),
                "artifact_id": trace.get("artifact_id"),
            },
        }
        self.audit_sink(event)
        self._observation_audit(event)

    def _observation_audit(self, event: dict) -> None:
        if self.observation_window is None:
            return
        handler = getattr(self.observation_window, "handle_llm_audit_event", None)
        if handler is None:
            return
        try:
            handler(event)
        except Exception:
            # Observation is a safety surface, but audit handler failures must
            # not corrupt the LLM response path.
            return

    @classmethod
    def default(
        cls,
        config: AppConfig,
        *,
        anthropic_executor: Callable[[LLMRequest], LLMResponse] | None = None,
        openai_transport: Callable[[LLMRequest], LLMResponse] | None = None,
        google_transport: Callable[[LLMRequest], LLMResponse] | None = None,
        ollama_transport: Callable[[LLMRequest], LLMResponse] | None = None,
        codex_transport: Callable[[LLMRequest], LLMResponse] | None = None,
        audit_sink: Callable[[dict], None] | None = None,
        pre_hooks: list[PreLLMHook] | None = None,
        post_hooks: list[PostLLMHook] | None = None,
        openai_tool_executor: Callable[[str, dict], dict] | None = None,
        openai_tool_schemas: list[dict] | None = None,
        observation_window: object | None = None,
    ) -> "LLMRouter":
        return cls(
            config=config,
            adapters={
                "anthropic": AnthropicAgentAdapter(executor=anthropic_executor),
                "openai": OpenAIAdapter(
                    transport=openai_transport,
                    api_key=config.openai_api_key,
                    tool_executor=openai_tool_executor,
                    tool_schemas=openai_tool_schemas,
                ),
                "google": GoogleAdapter(transport=google_transport, api_key=config.google_api_key),
                "ollama": OllamaAdapter(
                    transport=ollama_transport,
                    host=config.ollama_host,
                    num_ctx=min(config.worker_context_window, 131072),
                    think=True,
                ),
                "codex": CodexAdapter(
                    cli_path=config.codex_cli_path,
                    transport=codex_transport,
                ),
            },
            audit_sink=audit_sink,
            pre_hooks=pre_hooks,
            post_hooks=post_hooks,
            observation_window=observation_window,
        )

    def _validate_lane_input(
        self,
        lane: Lane,
        evidence_pack: dict | None,
        allowed_tools: list[str] | None,
        agents: dict | None,
        hooks: dict | None,
    ) -> None:
        if lane in self.NON_TOOL_LANES:
            if evidence_pack is None:
                raise ValueError(f"Lane '{lane}' requires an evidence_pack.")
            if any(value is not None for value in (allowed_tools, agents, hooks)):
                raise ValueError(f"Lane '{lane}' cannot receive tool-loop configuration.")


def _fallback_session_id(request: LLMRequest, fallback_provider: str) -> str | None:
    """Return a provider-safe session cursor for fallback requests."""
    session_id = request.session_id
    if not session_id:
        return None
    if fallback_provider == request.provider:
        return session_id
    if fallback_provider == "openai" and _looks_like_openai_response_id(session_id):
        return session_id
    return None


def _looks_like_openai_response_id(session_id: str) -> bool:
    return session_id.startswith("resp_") or session_id.startswith("resp-")


_INTERNAL_TOOL_TRACE_PATTERNS = (
    re.compile(r"(?<!\w)to=(?:functions|multi_tool_use|web|image_gen|tool_search)\.", re.IGNORECASE),
    re.compile(r'"recipient_name"\s*:\s*"(?:functions|multi_tool_use|web|image_gen|tool_search)\.', re.IGNORECASE),
    re.compile(r'"tool_uses"\s*:\s*\[', re.IGNORECASE),
)
_CORRUPT_PROVIDER_CONTENT = (
    "Tuve un error preparando la respuesta del modelo y suprimi contenido interno corrupto. "
    "No lo envio como resultado; conserva el estado y reintenta la accion con evidencia."
)


def _suppress_corrupt_provider_content(response: LLMResponse) -> None:
    content = response.content or ""
    stripped = content.strip()
    if stripped == "(unused)":
        response.content = ""
        response.confidence = 0.0
        response.artifacts["provider_placeholder_suppressed"] = True
        response.artifacts["contract_violation"] = "provider_unused_placeholder"
        return
    if any(pattern.search(content) for pattern in _INTERNAL_TOOL_TRACE_PATTERNS):
        response.content = _CORRUPT_PROVIDER_CONTENT
        response.confidence = 0.0
        response.artifacts["internal_tool_trace_suppressed"] = True
        response.artifacts["contract_violation"] = "internal_tool_trace"
        response.artifacts["raw_response"] = "[suppressed_internal_tool_trace]"


def _validate_provider_model_pair(provider: str, model: str) -> None:
    normalized_provider = provider.strip().lower()
    normalized_model = model.strip().lower()
    if normalized_provider == "anthropic" and (
        normalized_model.startswith("gpt-")
        or normalized_model.startswith("o3")
        or normalized_model.startswith("o4")
    ):
        raise ValueError(f"Anthropic provider cannot serve OpenAI model {model!r}.")
    if normalized_provider in {"openai", "codex"} and normalized_model.startswith("claude-"):
        raise ValueError(f"{provider} provider cannot serve Anthropic model {model!r}.")
    if normalized_provider == "google" and not normalized_model.startswith("gemini-"):
        raise ValueError(f"Google provider cannot serve non-Gemini model {model!r}.")


def _prompt_size_metadata(request: LLMRequest) -> dict[str, int | str | bool]:
    effective_input = build_effective_input(request)
    effective_system_prompt = build_effective_system_prompt(request)
    raw_evidence_chars = evidence_pack_serialized_chars(request.evidence_pack)
    effective_input_chars = _prompt_chars(effective_input)
    effective_system_chars = len(effective_system_prompt or "")
    return {
        "lane": request.lane,
        "provider": request.provider,
        "prompt_chars": _prompt_chars(request.prompt),
        "system_prompt_chars": len(request.system_prompt or ""),
        "evidence_pack_chars": raw_evidence_chars,
        "effective_input_chars": effective_input_chars,
        "effective_system_prompt_chars": effective_system_chars,
        "estimated_prompt_tokens": _estimated_tokens(_prompt_chars(request.prompt)),
        "estimated_system_prompt_tokens": _estimated_tokens(len(request.system_prompt or "")),
        "estimated_evidence_pack_tokens": _estimated_tokens(raw_evidence_chars),
        "estimated_effective_input_tokens": _estimated_tokens(effective_input_chars),
        "estimated_total_input_tokens": _estimated_tokens(effective_input_chars + effective_system_chars),
        "evidence_pack_truncated": (
            raw_evidence_chars > ADVISORY_EVIDENCE_PACK_MAX_CHARS
            and request.lane in ADVISORY_LANES
        ),
    }


def _prompt_chars(prompt: UserPrompt) -> int:
    if isinstance(prompt, str):
        return len(prompt)
    total = 0
    for block in prompt:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"text", "input_text"}:
            total += len(str(block.get("text") or ""))
        else:
            total += len(str(block))
    return total


def _estimated_tokens(chars: int) -> int:
    return max(int(chars) // 4, 1) if chars else 0


_TOTAL_TOKEN_KEYS = ("total_tokens", "total_token_count")
_INPUT_TOKEN_KEYS = (
    "input_tokens",
    "prompt_tokens",
    "prompt_token_count",
    "prompt_eval_count",
)
_OUTPUT_TOKEN_KEYS = (
    "output_tokens",
    "completion_tokens",
    "candidates_token_count",
    "eval_count",
)
_AUX_OUTPUT_TOKEN_KEYS = ("thoughts_token_count", "reasoning_tokens")
_CACHE_READ_TOKEN_KEYS = ("cache_read_input_tokens",)
_CACHE_CREATE_TOKEN_KEYS = ("cache_creation_input_tokens",)


def _token_usage_metadata(
    response: LLMResponse,
    request: LLMRequest | None,
    metadata: dict,
) -> dict[str, int | str | bool]:
    artifacts = response.artifacts if isinstance(response.artifacts, dict) else {}
    usage = artifacts.get("usage")
    reported = _reported_token_total(usage if isinstance(usage, dict) else {})
    source = "provider_usage"
    if reported["total_tokens"] <= 0:
        reported = _reported_token_total(artifacts)
        source = "response_artifacts"
    if reported["total_tokens"] > 0:
        return {
            **reported,
            "estimated": False,
            "source": source,
        }

    prompt_size = metadata.get("prompt_size") if isinstance(metadata.get("prompt_size"), dict) else {}
    input_tokens = _coerce_int(prompt_size.get("estimated_total_input_tokens"), 0)
    output_tokens = _estimated_tokens(len(response.content or ""))
    total_tokens = input_tokens + output_tokens
    if request is None and total_tokens <= 0:
        total_tokens = output_tokens
    return {
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated": True,
        "source": "prompt_size_estimate",
    }


def _reported_token_total(usage: dict[str, object]) -> dict[str, int | bool]:
    reported_total = _sum_token_keys(usage, _TOTAL_TOKEN_KEYS)
    raw_input_tokens = _sum_token_keys(usage, _INPUT_TOKEN_KEYS)
    output_tokens = _sum_token_keys(usage, _OUTPUT_TOKEN_KEYS) + _sum_token_keys(usage, _AUX_OUTPUT_TOKEN_KEYS)
    cache_read_tokens = _sum_token_keys(usage, _CACHE_READ_TOKEN_KEYS)
    cache_create_tokens = _sum_token_keys(usage, _CACHE_CREATE_TOKEN_KEYS)
    counted_input_tokens = raw_input_tokens + cache_create_tokens

    if reported_total > 0:
        total_tokens = max(reported_total - cache_read_tokens, 0)
        if cache_read_tokens > 0 and counted_input_tokens + output_tokens > 0:
            total_tokens = max(counted_input_tokens + output_tokens, 0)
        payload: dict[str, int | bool] = {
            "total_tokens": total_tokens,
            "input_tokens": max(total_tokens - output_tokens, 0),
            "output_tokens": output_tokens,
            "provider_total_tokens": reported_total,
        }
    else:
        total_tokens = counted_input_tokens + output_tokens
        provider_total = total_tokens + cache_read_tokens
        payload = {
            "total_tokens": total_tokens,
            "input_tokens": counted_input_tokens,
            "output_tokens": output_tokens,
            "provider_total_tokens": provider_total,
        }

    if cache_read_tokens > 0:
        payload["cache_read_input_tokens"] = cache_read_tokens
        payload["token_window_excludes_cache_read"] = True
    if cache_create_tokens > 0:
        payload["cache_creation_input_tokens"] = cache_create_tokens
    if raw_input_tokens > 0:
        payload["raw_input_tokens"] = raw_input_tokens
    return payload


def _sum_token_keys(usage: dict[str, object], keys: tuple[str, ...]) -> int:
    total = 0
    for key in keys:
        total += _coerce_int(usage.get(key), 0)
    return total


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
