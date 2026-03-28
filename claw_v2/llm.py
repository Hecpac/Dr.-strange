from __future__ import annotations

from dataclasses import asdict
from typing import Callable

from claw_v2.adapters.anthropic import AnthropicAgentAdapter
from claw_v2.adapters.base import AdapterError, LLMRequest, PostLLMHook, PreLLMHook, ProviderAdapter, UserPrompt
from claw_v2.adapters.google import GoogleAdapter
from claw_v2.adapters.openai import OpenAIAdapter
from claw_v2.config import AppConfig
from claw_v2.types import Lane, LLMResponse


class LLMRouter:
    """Multi-lane router with explicit fallback to Anthropic for secondary lanes."""

    NON_TOOL_LANES: tuple[Lane, ...] = ("verifier", "research", "judge")

    def __init__(
        self,
        config: AppConfig,
        adapters: dict[str, ProviderAdapter],
        audit_sink: Callable[[dict], None] | None = None,
        pre_hooks: list[PreLLMHook] | None = None,
        post_hooks: list[PostLLMHook] | None = None,
    ) -> None:
        self.config = config
        self.adapters = adapters
        self.audit_sink = audit_sink or (lambda event: None)
        self.pre_hooks: list[PreLLMHook] = list(pre_hooks or [])
        self.post_hooks: list[PostLLMHook] = list(post_hooks or [])

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
        timeout: float = 120.0,
    ) -> LLMResponse:
        self._validate_lane_input(lane, evidence_pack, allowed_tools, agents, hooks)
        selected_provider = provider or self.config.provider_for_lane(lane)
        selected_model = model or self.config.model_for_lane(lane)
        selected_effort = effort or self.config.effort_for_lane(lane)
        budget = max_budget if max_budget is not None else self.config.max_budget_usd
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
        )
        # --- Pre-hooks ---
        for hook in self.pre_hooks:
            result = hook(request)
            if result is None:
                return LLMResponse(
                    content="Request blocked by pre-hook.",
                    lane=request.lane,
                    provider="none",
                    model="none",
                    confidence=0.0,
                    cost_estimate=0.0,
                    artifacts={"blocked_by": getattr(hook, "__name__", "pre_hook")},
                )
            request = result

        adapter = self._adapter_for(selected_provider)
        if lane not in self.NON_TOOL_LANES and not adapter.tool_capable:
            raise ValueError(f"Lane '{lane}' requires a tool-capable provider adapter.")

        try:
            response = adapter.complete(request)
        except AdapterError as exc:
            if lane in self.NON_TOOL_LANES and selected_provider != "anthropic":
                fallback_request = LLMRequest(
                    **{**asdict(request), "provider": "anthropic", "model": self.config.model_for_lane(lane)}
                )
                response = self._adapter_for("anthropic").complete(fallback_request)
                response.degraded_mode = True
                response.artifacts["fallback_reason"] = str(exc)
                self._audit("llm_fallback", response, {"requested_provider": selected_provider})
                return response
            raise

        # --- Post-hooks ---
        for hook in self.post_hooks:
            response = hook(request, response)

        self._audit("llm_response", response, {"session_id": session_id})
        return response

    def _adapter_for(self, provider: str) -> ProviderAdapter:
        if provider not in self.adapters:
            raise AdapterError(f"No adapter registered for provider '{provider}'.")
        return self.adapters[provider]

    def _audit(self, action: str, response: LLMResponse, metadata: dict) -> None:
        self.audit_sink(
            {
                "action": action,
                "lane": response.lane,
                "provider": response.provider,
                "model": response.model,
                "cost_estimate": response.cost_estimate,
                "confidence": response.confidence,
                "degraded_mode": response.degraded_mode,
                "metadata": metadata,
            }
        )

    @classmethod
    def default(
        cls,
        config: AppConfig,
        *,
        anthropic_executor: Callable[[LLMRequest], LLMResponse] | None = None,
        openai_transport: Callable[[LLMRequest], LLMResponse] | None = None,
        google_transport: Callable[[LLMRequest], LLMResponse] | None = None,
        audit_sink: Callable[[dict], None] | None = None,
        pre_hooks: list[PreLLMHook] | None = None,
        post_hooks: list[PostLLMHook] | None = None,
    ) -> "LLMRouter":
        return cls(
            config=config,
            adapters={
                "anthropic": AnthropicAgentAdapter(executor=anthropic_executor),
                "openai": OpenAIAdapter(transport=openai_transport, api_key=config.openai_api_key),
                "google": GoogleAdapter(transport=google_transport, api_key=config.google_api_key),
            },
            audit_sink=audit_sink,
            pre_hooks=pre_hooks,
            post_hooks=post_hooks,
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
