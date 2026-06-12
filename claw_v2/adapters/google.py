from __future__ import annotations

from importlib import import_module
from typing import Callable

from claw_v2.adapters.base import (
    AdapterError,
    AdapterUnavailableError,
    LLMRequest,
    ProviderAdapter,
    build_effective_input,
    build_effective_system_prompt,
    coerce_usage_dict,
)
from claw_v2.pricing import estimate_cost_usd
from claw_v2.types import LLMResponse


class GoogleAdapter(ProviderAdapter):
    """Advisory-only adapter — D6 decision (2026-06-12): documented, not pruned.

    Google/Gemini serves only the advisory lanes (verifier/research/judge):
    ``tool_capable`` stays False, there is no tool loop, no session reuse and
    no fallback chain points here. It remains available as a cheap advisory
    provider option; giving it a tool loop would be a new project, not a
    flag flip.
    """

    provider_name = "google"
    tool_capable = False

    def __init__(
        self,
        transport: Callable[[LLMRequest], LLMResponse] | None = None,
        *,
        api_key: str | None = None,
    ) -> None:
        self._transport = transport
        self._api_key = api_key

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._transport is not None:
            return self._transport(request)
        genai = self._load_genai()
        types = import_module("google.genai.types")
        client = genai.Client(api_key=self._api_key) if self._api_key else genai.Client()
        config = None
        system_prompt = build_effective_system_prompt(request)
        if system_prompt:
            config = types.GenerateContentConfig(system_instruction=system_prompt)
        try:
            response = client.models.generate_content(
                model=request.model,
                contents=build_effective_input(request),
                config=config,
            )
        except Exception as exc:  # pragma: no cover - live SDK path
            raise AdapterError(f"Google GenAI request failed: {exc}") from exc
        usage = coerce_usage_dict(getattr(response, "usage_metadata", None))
        estimate = estimate_cost_usd("google", request.model, usage)
        return LLMResponse(
            content=(getattr(response, "text", None) or "").strip(),
            lane=request.lane,
            provider="google",
            model=request.model,
            confidence=0.7 if getattr(response, "text", None) else 0.0,
            cost_estimate=estimate.amount_usd,
            cost_unknown=estimate.unknown,
            cost_source=estimate.price_source,
            cost_price_as_of=estimate.price_as_of,
            artifacts={
                "response_id": getattr(response, "response_id", None),
                "usage": usage,
            },
        )

    @staticmethod
    def _load_genai():
        try:
            return import_module("google.genai")
        except ModuleNotFoundError as exc:
            raise AdapterUnavailableError(
                "google.genai is not installed. Install the 'google-genai' Python package to enable Google provider support."
            ) from exc
